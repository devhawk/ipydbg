import clr
clr.AddReference('CorDebug')

import sys

from System import Array, Console, ConsoleKey, ConsoleModifiers 
from System.IO import Path
from System.Reflection import Assembly
from System.Threading import WaitHandle, AutoResetEvent
from System.Threading import Thread, ApartmentState, ParameterizedThreadStart
from System.Diagnostics.SymbolStore import ISymbolDocument, SymbolToken

from Microsoft.Samples.Debugging.CorDebug import CorDebugger, CorFrameType
from Microsoft.Samples.Debugging.CorDebug.NativeApi import CorDebugUnmappedStop, COR_DEBUG_STEP_RANGE
from Microsoft.Samples.Debugging.CorMetadata import CorMetadataImport
from Microsoft.Samples.Debugging.CorMetadata.NativeApi import IMetadataImport
from Microsoft.Samples.Debugging.CorSymbolStore import SymbolBinder

#--------------------------------------------
# sequence point functions

class sequence_point(object):
  def __init__(self, offset, doc, start_line, start_col, end_line, end_col):
    self.offset = offset
    self.doc = doc
    self.start_line = start_line
    self.start_col = start_col
    self.end_line = end_line
    self.end_col = end_col
    
  def __str__(self):
    return "%s:%d (offset: %d)" % (Path.GetFileName(self.doc.URL), self.start_line, self.offset)
    
def get_sequence_points(method):
  sp_count = method.SequencePointCount
  spOffsets    = Array.CreateInstance(int, sp_count)
  spDocs = Array.CreateInstance(ISymbolDocument, sp_count)
  spStartLines = Array.CreateInstance(int, sp_count)
  spEndLines   = Array.CreateInstance(int, sp_count)
  spStartCol   = Array.CreateInstance(int, sp_count)
  spEndCol     = Array.CreateInstance(int, sp_count)
  
  method.GetSequencePoints(spOffsets, spDocs, spStartLines, spStartCol, 
                           spEndLines, spEndCol)

  for i in range(sp_count):
    if spStartLines[i] != 0xfeefee:
      yield sequence_point(spOffsets[i], spDocs[i], spStartLines[i], 
                           spStartCol[i], spEndLines[i], spEndCol[i])

  
#--------------------------------------------
# breakpoint funcitons

def create_breakpoint(doc, line, module, reader):
  line = doc.FindClosestLine(line)
  method = reader.GetMethodFromDocumentPosition(doc, line, 0)
  function = module.GetFunctionFromToken(method.Token.GetToken())
  
  for sp in get_sequence_points(method):
    if sp.doc.URL == doc.URL and sp.start_line == line:
      bp = function.ILCode.CreateBreakpoint(sp.offset)
      bp.Activate(True)
      return bp
      
  bp = function.CreateBreakpoint()
  bp.Activate(True)
  return bp

#--------------------------------------------
# frame functions

def get_method_info_for_frame(frame):
    if frame.FrameType != CorFrameType.ILFrame:
      return None
    metadata_import = CorMetadataImport(frame.Function.Module)
    return metadata_import.GetMethodInfo(frame.FunctionToken)
    
def get_dynamic_frames(chain):
  for f in chain.Frames:
    method_info = get_method_info_for_frame(f)
    if method_info == None:
      continue
    typename = method_info.DeclaringType.Name
    if typename.startswith("Microsoft.Scripting.") \
      or typename.startswith("IronPython.") \
      or typename == "PythonConsoleHost":
        continue
    yield f
    
#--------------------------------------------
# stepper functions

def create_stepper(thread, JMC = True):
  stepper = thread.ActiveFrame.CreateStepper()
  stepper.SetUnmappedStopMask(CorDebugUnmappedStop.STOP_NONE)
  stepper.SetJmcStatus(JMC)
  return stepper
  
from System import UInt32
def create_step_range(start, end):
  range = Array.CreateInstance(COR_DEBUG_STEP_RANGE, 1)
  range[0] = COR_DEBUG_STEP_RANGE( 
                startOffset = UInt32(start), 
                endOffset = UInt32(end))
  return range
  
def get_step_ranges(thread, reader):
    frame = thread.ActiveFrame
    offset, mapResult = frame.GetIP()
    method = reader.GetMethod(SymbolToken(frame.FunctionToken))
    for sp in get_sequence_points(method):
        if sp.offset > offset:
            return create_step_range(offset, sp.offset)
    return create_step_range(offset, frame.Function.ILCode.Size)
  
infrastructure_methods =  ['TryGetExtraValue', 
    'TrySetExtraValue', 
    '.cctor', 
    '.ctor', 
    'CustomSymbolDictionary.GetExtraKeys', 
    'IModuleDictionaryInitialization.InitializeModuleDictionary']
                  
#--------------------------------------------
# main IPyDebugProcess class
  
class IPyDebugProcess(object):
    def __init__(self, debugger=None):
        self.debugger = debugger if debugger != None \
            else CorDebugger(CorDebugger.GetDefaultDebuggerVersion())
            
    def run(self, py_file):
        self.py_file = py_file
        #use the current executing version of IPY to launch the debug process
        ipy = Assembly.GetEntryAssembly().Location
        cmd_line = "\"%s\" -D \"%s\"" % (ipy, py_file)
        self.process = self.debugger.CreateProcess(ipy, cmd_line)
        
        self.process.OnCreateAppDomain += self.OnCreateAppDomain
        self.process.OnProcessExit += self.OnProcessExit
        self.process.OnUpdateModuleSymbols += self.OnUpdateModuleSymbols
        self.process.OnBreakpoint += self.OnBreakpoint
        self.process.OnStepComplete += self.OnStepComplete
        self.process.OnClassLoad += self.OnClassLoad
        
        self.terminate_event = AutoResetEvent(False)
        self.break_event = AutoResetEvent(False)

        self.sym_binder = SymbolBinder()
        self.initial_breakpoint = None
        self.symbol_readers = dict()

        handles = Array.CreateInstance(WaitHandle, 2)
        handles[0] = self.terminate_event
        handles[1] = self.break_event

        while True:
            if hasattr(self, 'active_thread'): delattr(self, 'active_thread')
            if hasattr(self, 'active_appdomain'): delattr(self, 'active_appdomain')
            self.process.Continue(False)
            i = WaitHandle.WaitAny(handles)
            if i == 0:
                break
            self._input()
            
        
    def _input(self):
        while True:
            print "» ",
            k = Console.ReadKey()

            if k.Key == ConsoleKey.Spacebar:
                print "\nContinuing"
                return
            elif k.Key == ConsoleKey.Q:
                print "\nQuitting"
                self.process.Stop(0)
                self.process.Terminate(255)
                return
            elif k.Key == ConsoleKey.T:
                print "\nStack Trace"
                get_frames = get_dynamic_frames(self.active_thread.ActiveChain) \
                    if (k.Modifiers & ConsoleModifiers.Alt) != ConsoleModifiers.Alt \
                    else self.active_thread.ActiveChain.Frames
                for f in get_frames:
                    offset, sp = self._get_location(f)
                    method_info = get_method_info_for_frame(f)
                    print "  ",
                    if method_info != None:
                      print "%s::%s --" % (method_info.DeclaringType.Name, method_info.Name),
                    print sp if sp != None else "(offset %d)" % offset
            elif k.Key == ConsoleKey.S:
                print "\nStepping"
                self._do_step(False)
                return
            elif k.Key == ConsoleKey.I:
                print "\nStepping In"
                self._do_step(True)
                return                
            elif k.Key == ConsoleKey.O:
                print "\nStepping Out"
                stepper = create_stepper(self.active_thread)
                stepper.StepOut()
                return
            else:
                print "\nPlease enter a valid command"
        
    def OnCreateAppDomain(self, sender,e):
        print "OnCreateAppDomain", e.AppDomain.Name
        e.AppDomain.Attach()
  
    def OnProcessExit(self, sender,e):
        print "OnProcessExit"
        self.terminate_event.Set()
   
    def OnClassLoad(self, sender, e):
        cmi = CorMetadataImport(e.Class.Module)
        mt = cmi.GetType(e.Class.Token)
        print "OnClassLoad", mt.Name
        
        #python code is always in a dynamic module, 
        #so non-dynamic modules aren't JMC
        if not e.Class.Module.IsDynamic:
          e.Class.JMCStatus = False
        
        #python classes in the IronPython.NewTypes only implement python class 
        #semantics, they have no python code in them so they aren't JMC
        elif mt.Name.startswith('IronPython.NewTypes'):
          e.Class.JMCStatus = False
          
        #assume that dynamic module classes not in the IronPython.NewTypes 
        #namespace are python modules, so mark them as JMC and iterate thru
        #the methods looking for standard infrastructure methods to mark as
        #JMC disabled
        else:
          e.Class.JMCStatus = True
          
          for mmi in mt.GetMethods():
            if mmi.Name in infrastructure_methods:
              f = e.Class.Module.GetFunctionFromToken(mmi.MetadataToken)
              f.JMCStatus = False

    def OnUpdateModuleSymbols(self, sender,e):
        print "OnUpdateModuleSymbols"

        metadata_import = e.Module.GetMetaDataInterface[IMetadataImport]()
        reader = self.sym_binder.GetReaderFromStream(metadata_import, e.Stream)

        self.symbol_readers[e.Module] = reader
        if self.initial_breakpoint != None:
            return

        full_path = Path.GetFullPath(self.py_file)
        for doc in reader.GetDocuments():
            if str.IsNullOrEmpty(doc.URL):
                continue
            if str.Compare(full_path, Path.GetFullPath(doc.URL), True) == 0:
                self.initial_breakpoint = create_breakpoint(doc, 1, e.Module, reader)

    def OnBreakpoint(self, sender,e):
        func = e.Thread.ActiveFrame.Function
        metadata_import = CorMetadataImport(func.Module)
        method_info = metadata_import.GetMethodInfo(func.Token)

        offset, sp = self._get_location(e.Thread.ActiveFrame)
        print "OnBreakpoint", method_info.Name, "Location:", sp if sp != None else "offset %d" % offset
        self._do_break_event(e)

    def OnStepComplete(self, sender,e):
        offset, sp = self._get_location(e.Thread.ActiveFrame)
        print "OnStepComplete Reason:", e.StepReason, "Location:", sp if sp != None else "offset %d" % offset
        self._do_break_event(e)
  
    def _do_break_event(self, e):
        self.active_appdomain = e.AppDomain
        self.active_thread = e.Thread
        e.Continue = False
        self.break_event.Set()
        
    def _get_location(self, frame):
        offset, mapping_result = frame.GetIP()
  
        if frame.FrameType != CorFrameType.ILFrame:
            return offset, None
        if frame.Function.Module not in self.symbol_readers:
            return offset, None
    
        reader = self.symbol_readers[frame.Function.Module]
        method = reader.GetMethod(SymbolToken(frame.FunctionToken))
  
        real_sp = None
        for sp in get_sequence_points(method):
            if sp.offset > offset: 
                break
            real_sp = sp
      
        if real_sp == None:
            return offset, None
  
        return offset, real_sp
        
    def _do_step(self, step_in):
        stepper = create_stepper(self.active_thread)
        module = self.active_thread.ActiveFrame.Function.Module
        if module not in self.symbol_readers:
            stepper.Step(step_in)
        else:
          range = get_step_ranges(self.active_thread, self.symbol_readers[module])
          stepper.StepRange(step_in, range)

      

def run_debugger(py_file):
    if Thread.CurrentThread.GetApartmentState() == ApartmentState.STA:
        t = Thread(ParameterizedThreadStart(run_debugger))
        t.SetApartmentState(ApartmentState.MTA)
        t.Start(py_file)
        t.Join()   
    else:
        p = IPyDebugProcess()
        p.run(py_file)

if __name__ == "__main__":        

    run_debugger(sys.argv[1])        


