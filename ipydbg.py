from __future__ import with_statement

import clr
clr.AddReference('CorDebug')

import sys

from System import Array, Console, ConsoleKey, ConsoleModifiers, ConsoleColor
from System.IO import Path, File
from System.Reflection import Assembly
from System.Threading import WaitHandle, AutoResetEvent
from System.Threading import Thread, ApartmentState, ParameterizedThreadStart
from System.Diagnostics.SymbolStore import ISymbolDocument, SymbolToken

from Microsoft.Samples.Debugging.CorDebug import CorDebugger, CorFrameType
from Microsoft.Samples.Debugging.CorDebug.NativeApi import CorDebugUnmappedStop, COR_DEBUG_STEP_RANGE, CorDebugStepReason
from Microsoft.Samples.Debugging.CorMetadata import CorMetadataImport
from Microsoft.Samples.Debugging.CorMetadata.NativeApi import IMetadataImport
from Microsoft.Samples.Debugging.CorSymbolStore import SymbolBinder

import consolecolor as CC

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
    return "%s %d:%d-%d:%d (offset:%d)" % (Path.GetFileName(self.doc.URL), 
      self.start_line, self.start_col, self.end_line, self.end_col, self.offset)
    
def get_sequence_points(symmethod, include_hidden_lines = False):
  sp_count     = symmethod.SequencePointCount
  spOffsets    = Array.CreateInstance(int, sp_count)
  spDocs       = Array.CreateInstance(ISymbolDocument, sp_count)
  spStartLines = Array.CreateInstance(int, sp_count)
  spEndLines   = Array.CreateInstance(int, sp_count)
  spStartCol   = Array.CreateInstance(int, sp_count)
  spEndCol     = Array.CreateInstance(int, sp_count)
  
  symmethod.GetSequencePoints(spOffsets, spDocs, spStartLines, spStartCol, 
                              spEndLines, spEndCol)

  for i in range(sp_count):
    if spStartLines[i] != 0xfeefee or include_hidden_lines:
      yield sequence_point(spOffsets[i], spDocs[i], spStartLines[i], 
                           spStartCol[i], spEndLines[i], spEndCol[i])

  
#--------------------------------------------
# breakpoint funcitons

def create_breakpoint(doc, line, module):
  line = doc.FindClosestLine(line)
  method = module.SymbolReader.GetMethodFromDocumentPosition(doc, line, 0)
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

def get_dynamic_frames(chain):
  for f in chain.Frames:
    method_info = f.GetMethodInfo()
    if method_info == None:
      continue
    typename = method_info.DeclaringType.Name
    if typename.startswith("Microsoft.Scripting.") \
      or typename.startswith("IronPython.") \
      or typename == "PythonConsoleHost":
        continue
    yield f

def get_location(function, offset):
    reader = function.Module.SymbolReader
    if reader == None:  
        return None
    method = reader.GetMethod(SymbolToken(function.Token))

    prev_sp = None
    for sp in get_sequence_points(method):
        if sp.offset > offset: 
            break
        prev_sp = sp
    return prev_sp

def get_frame_location(frame):
    offset, mapping_result = frame.GetIP()

    if frame.FrameType != CorFrameType.ILFrame:
        return offset, None
    return offset, get_location(frame.Function, offset)
    
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
  
def do_step(thread, step_in):
    stepper = create_stepper(thread)
    reader = thread.ActiveFrame.Function.Module.SymbolReader
    if reader == None:
        stepper.Step(step_in)
    else:
      range = get_step_ranges(thread, reader)
      stepper.StepRange(step_in, range)      
      
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

        self.initial_breakpoint = None
        self.source_files = dict()

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

    def _print_source_line(self, sp, lines):
      linecount = len(lines)
      linecountstr = "%" + str(len(str(linecount))) + "d: "

      for i in range(sp.start_line, sp.end_line+1):
        with CC.Cyan:
          Console.Write(linecountstr % i)
        line = lines[i-1] if i <= linecount else ""
        start = sp.start_col if i==sp.start_line else 1
        end = sp.end_col if i == sp.end_line else len(line)+1
        
        with CC.Gray:
          Console.Write(line.Substring(0, start-1))
          with CC.Yellow:
            Console.Write(line.Substring(start-1, end-start))
          Console.Write(line.Substring(end-1))

        if sp.start_line == sp.end_line == i and sp.start_col == sp.end_col:
          with CC.Yellow: Console.Write(" ^^^")
        Console.WriteLine()

    def _input(self):
        offset, sp = get_frame_location(self.active_thread.ActiveFrame)
        lines = self._get_file(sp.doc.URL)
        self._print_source_line(sp, lines)
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
                    offset, sp = get_frame_location(f)
                    method_info = f.GetMethodInfo()
                    print "  ",
                    if method_info != None:
                      print "%s::%s --" % (method_info.DeclaringType.Name, method_info.Name),
                    print sp if sp != None else "(offset %d)" % offset, f.FrameType
            elif k.Key == ConsoleKey.S:
                print "\nStepping"
                do_step(self.active_thread, False)
                return
            elif k.Key == ConsoleKey.I:
                print "\nStepping In"
                do_step(self.active_thread, True)
                return                
            elif k.Key == ConsoleKey.O:
                print "\nStepping Out"
                stepper = create_stepper(self.active_thread)
                stepper.StepOut()
                return
            else:
                print "\nPlease enter a valid command"
        
    def OnCreateAppDomain(self, sender,e):
        with CC.DarkGray: 
          print "OnCreateAppDomain", e.AppDomain.Name
        e.AppDomain.Attach()
  
    def OnProcessExit(self, sender,e):
        with CC.DarkGray:
          print "OnProcessExit"
        self.terminate_event.Set()
   
    infrastructure_methods =  ['TryGetExtraValue', 
      'TrySetExtraValue', 
      '.cctor', 
      '.ctor', 
      'CustomSymbolDictionary.GetExtraKeys', 
      'IModuleDictionaryInitialization.InitializeModuleDictionary']
      
    def OnClassLoad(self, sender, e):
        mt = e.Class.GetTypeInfo()
        with CC.DarkGray:
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
            if mmi.Name in IPyDebugProcess.infrastructure_methods:
              f = e.Class.Module.GetFunctionFromToken(mmi.MetadataToken)
              f.JMCStatus = False

    def OnUpdateModuleSymbols(self, sender,e):
        with CC.DarkGray:
          print "OnUpdateModuleSymbols", e.Module.Name

        e.Module.UpdateSymbolReaderFromStream(e.Stream)
        if self.initial_breakpoint != None:
            return

        full_path = Path.GetFullPath(self.py_file)
        for doc in e.Module.SymbolReader.GetDocuments():
            if str.IsNullOrEmpty(doc.URL):
                continue
            if str.Compare(full_path, Path.GetFullPath(doc.URL), True) == 0:
                self.initial_breakpoint = create_breakpoint(doc, 1, e.Module)

    def OnBreakpoint(self, sender,e):
        method_info =  e.Thread.ActiveFrame.Function.GetMethodInfo()
        offset, sp = get_frame_location(e.Thread.ActiveFrame)
        with CC.DarkGray:
          print "OnBreakpoint", method_info.Name, "Location:", sp if sp != None else "offset %d" % offset
        self._do_break_event(e)

    def OnStepComplete(self, sender,e):
        offset, sp = get_frame_location(e.Thread.ActiveFrame)
        with CC.DarkGray:
          print "OnStepComplete Reason:", e.StepReason, "Location:", sp if sp != None else "offset %d" % offset
        if e.StepReason == CorDebugStepReason.STEP_CALL:
          do_step(e.Thread, False)
        else:
          self._do_break_event(e)
            
    def _do_break_event(self, e):
        self.active_appdomain = e.AppDomain
        self.active_thread = e.Thread
        e.Continue = False
        self.break_event.Set()
        
    def _get_file(self,filename):
        filename = Path.GetFullPath(filename)
        if not filename in self.source_files:
          self.source_files[filename] = File.ReadAllLines(filename)
        return self.source_files[filename] 
    

      

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


