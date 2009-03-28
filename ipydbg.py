from __future__ import with_statement

import clr
clr.AddReference('CorDebug')

import sys

from System import Array, Console, ConsoleKey, ConsoleModifiers, ConsoleColor
from System.IO import Path, File
from System.Reflection import Assembly
from System.Threading import WaitHandle, AutoResetEvent
from System.Threading import Thread, ApartmentState, ParameterizedThreadStart
from System.Diagnostics.SymbolStore import ISymbolDocument

from Microsoft.Samples.Debugging.CorDebug import (CorDebugger, CorFrameType, 
  CorValue, CorReferenceValue, CorObjectValue, CorAppDomain, CorModule)
from Microsoft.Samples.Debugging.CorDebug.NativeApi import \
  CorDebugUnmappedStop, COR_DEBUG_STEP_RANGE, CorDebugStepReason
from Microsoft.Samples.Debugging.CorDebug.NativeApi.CorElementType import *

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

def create_breakpoint(module, filename, linenum):
    reader = module.SymbolReader
    if reader == None:
      return None
    
    # currently, I'm only comparing filenames. This algorithm may need to get more
    # sophisticated to support differntiating files with the same name in different paths
    filename = Path.GetFileName(filename)
    for doc in reader.GetDocuments():
      if str.Compare(filename, Path.GetFileName(doc.URL), True) == 0:
        linenum = doc.FindClosestLine(linenum)
        method = module.SymbolReader.GetMethodFromDocumentPosition(doc, linenum, 0)
        function = module.GetFunctionFromToken(method.Token.GetToken())
        
        for sp in get_sequence_points(method):
          if sp.doc.URL == doc.URL and sp.start_line == linenum:
            return function.ILCode.CreateBreakpoint(sp.offset)
        
        return function.CreateBreakpoint()

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
    symmethod = function.GetSymbolMethod()
    if symmethod == None:
      return None

    prev_sp = None
    for sp in get_sequence_points(symmethod):
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
    symmethod = frame.Function.GetSymbolMethod()
    for sp in get_sequence_points(symmethod):
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
# value functions

def get_locals(frame, scope=None, offset = None):
    #if the scope is unspecified, try and get it from the frame
    if scope == None:
        symmethod = frame.Function.GetSymbolMethod()
        if symmethod != None:
            scope = symmethod.RootScope
        #if scope still not available, yield the local variables
        #from the frame, with auto-gen'ed names (local_1, etc)
        else:
          for i in range(frame.GetLocalVariablesCount()):
            yield "local_%d" % i, frame.GetLocalVariable(i)
          return

    #if we have a scope, get the locals from the scope 
    #and their values from the frame
    for lv in scope.GetLocals():
        #always skip $site locals - they are cached callsites and 
        #not relevant to the ironpython developer
        if lv.Name == "$site": 
          continue
        v = frame.GetLocalVariable(lv.AddressField1)
        yield lv.Name, v

    if offset == None: offset = frame.GetIP()[0]

    #recusively call get_locals for all the child scopes
    for s in scope.GetChildren():
      if s.StartOffset <= offset and s.EndOffset >= offset:
        for ret in get_locals(frame, s, offset): yield ret

def get_arguments(frame):
    mi = frame.GetMethodInfo()
    for pi in mi.GetParameters():
      if pi.Position == 0: continue
      arg = frame.GetArgument(pi.Position - 1)
      yield pi.Name, arg


_type_map = { 
  'System.Boolean': ELEMENT_TYPE_BOOLEAN,
  'System.SByte'  : ELEMENT_TYPE_I1, 
  'System.Byte'   : ELEMENT_TYPE_U1,
  'System.Int16'  : ELEMENT_TYPE_I2, 
  'System.UInt16' : ELEMENT_TYPE_U2,
  'System.Int32'  : ELEMENT_TYPE_I4,
  'System.UInt32' : ELEMENT_TYPE_U4,
  'System.IntPtr' : ELEMENT_TYPE_I, 
  'System.UIntPtr': ELEMENT_TYPE_U,                  
  'System.Int64'  : ELEMENT_TYPE_I8, 
  'System.UInt64' : ELEMENT_TYPE_U8,
  'System.Single' : ELEMENT_TYPE_R4,
  'System.Double' : ELEMENT_TYPE_R8,
  'System.Char'   : ELEMENT_TYPE_CHAR, }
  
_generic_element_types = _type_map.values()


class NullCorValue(object):
  def __init__(self, typename):
    self.typename = typename
    
def extract_value(value):
    rv = value.CastToReferenceValue()
    if rv != None:
      if rv.IsNull: 
        typename = rv.ExactType.Class.GetTypeInfo().Name
        return NullCorValue(typename)
      return extract_value(rv.Dereference())
    bv = value.CastToBoxValue()
    if bv != None:
      return extract_value(bv.GetObject())

    if value.Type in _generic_element_types:
      return value.CastToGenericValue().GetValue()
    elif value.Type == ELEMENT_TYPE_STRING:
      return value.CastToStringValue().String
    elif value.Type == ELEMENT_TYPE_VALUETYPE:
      typename = value.ExactType.Class.GetTypeInfo().Name 
      if typename in _type_map:
        gv = value.CastToGenericValue()
        return gv.UnsafeGetValueAsType(_type_map[typename])
      else:
        return value.CastToObjectValue()
    elif value.Type in [ELEMENT_TYPE_CLASS, ELEMENT_TYPE_OBJECT]:
      return value.CastToObjectValue()
    else:
      raise (Exception,
        "<processing CorValue of type: %s not implemented>" % str(value.Type))

def display_value(value):
  if type(value) == str:
    return (('"%s"' % value), 'System.String')
  elif type(value) == CorObjectValue:
    return ("<...>", value.ExactType.Class.GetTypeInfo().FullName)
  elif type(value) == NullCorValue:
    return ("<None>", value.typename)
  else:
    return (str(value), value.GetType().FullName)

#--------------------------------------------
# main IPyDebugProcess class

def inputcmd(cmddict, key):
  def deco(f):
    cmddict[key] = f
    return f
  return deco
   
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
        self.breakpoints = []
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
      linecount_fmt = "%%%dd: " % len(str(linecount))

      for i in range(sp.start_line, sp.end_line+1):
        with CC.Cyan:
          Console.Write(linecount_fmt % i)
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

    _inputcmds = dict()
    _breakpointcmds = dict()
    
    @inputcmd(_inputcmds, ConsoleKey.R)
    def _input_repl_cmd(self, keyinfo):
      with CC.Gray:
        print "\nREPL Console\nPress Ctl-Z to Exit"
        cmd = ""
        _locals = {'self': self}

        while True:
          Console.Write(">>>" if not cmd else "...")
          
          line = Console.ReadLine()
          if line == None:
            break
          
          if line:
            cmd = cmd + line + "\n"
          else:
            try:
              if len(cmd) > 0:
                exec compile(cmd, "<input>", "single") in globals(),_locals
            except Exception, ex:
              with CC.Red: print type(ex), ex
            cmd = ""
            
    @inputcmd(_inputcmds, ConsoleKey.Spacebar)
    def _input_continue_cmd(self, keyinfo):
      print "\nContinuing"
      return True

    @inputcmd(_inputcmds, ConsoleKey.Q)
    def _input_quit_cmd(self, keyinfo):
      print "\nQuitting"
      self.process.Stop(0)
      self.process.Terminate(255)
      return True

    @inputcmd(_inputcmds, ConsoleKey.L)
    def _input_locals_cmd(self, keyinfo):
      def print_value(name, value):
        display, type_name = display_value(extract_value(value))
        with CC.Magenta: print "  ", name, 
        print display,
        with CC.Green: print type_name
        
      def print_all_values(f, show_hidden):
          count = 0
          for name,value in f(self.active_thread.ActiveFrame):
            if name.startswith("$") and not show_hidden:
              continue
            print_value(name, value)
            count+=1        
          return count
          
      print "\nLocals"
      show_hidden = (keyinfo.Modifiers & ConsoleModifiers.Alt) == ConsoleModifiers.Alt
      count = print_all_values(get_locals, show_hidden)
      count += print_all_values(get_arguments, show_hidden)

      if count == 0:
          with CC.Magenta: print "  No Locals Found" 

    @inputcmd(_inputcmds, ConsoleKey.T)
    def _input_stack_trace_cmd(self, keyinfo):
      print "\nStack Trace"
      get_frames = get_dynamic_frames(self.active_thread.ActiveChain) \
          if (keyinfo.Modifiers & ConsoleModifiers.Alt) != ConsoleModifiers.Alt \
          else self.active_thread.ActiveChain.Frames
      for f in get_frames:
          offset, sp = get_frame_location(f)
          method_info = f.GetMethodInfo()
          print "  ",
          if method_info != None:
            print "%s::%s --" % (method_info.DeclaringType.Name, method_info.Name),
          print sp if sp != None else "(offset %d)" % offset, f.FrameType
      return False
      
    @inputcmd(_inputcmds, ConsoleKey.S)
    def _input_step_over_cmd(self, keyinfo):
      print "\nStepping"
      do_step(self.active_thread, False)
      return True
      
    @inputcmd(_inputcmds, ConsoleKey.I)
    def _input_step_in_cmd(self, keyinfo):
      print "\nStepping In"
      do_step(self.active_thread, True)
      return True
      
    @inputcmd(_inputcmds, ConsoleKey.O)
    def _input_step_out_cmd(self, keyinfo):
      print "\nStepping Out"
      stepper = create_stepper(self.active_thread)
      stepper.StepOut()
      return True
      
    @inputcmd(_breakpointcmds, ConsoleKey.A)
    def _bp_add(self, keyinfo):
      try:
        args = Console.ReadLine().Trim().split(':')
        if len(args) != 2: raise Exception, "Only pass two arguments" 
        linenum = int(args[1])
        
        for assm in self.active_appdomain.Assemblies:
          for mod in assm.Modules:
              bp = create_breakpoint(mod, args[0], linenum)
              if bp != None:
                self.breakpoints.append(bp)
                bp.Activate(True)
                Console.WriteLine( "Breakpoint set")
                return False
        raise Exception, "Couldn't find %s:%d" % (args[0], linenum)    

      except Exception, msg:
        with CC.Red:
          print "Add breakpoint failed", msg

    @inputcmd(_breakpointcmds, ConsoleKey.L)
    def _bp_list(self, keyinfo):
      print "\nList Breakpoints"   
      for i, bp in enumerate(self.breakpoints): 
        sp = get_location(bp.Function, bp.Offset)
        state = "Active" if bp.IsActive else "Inactive"
        print "  %d. %s:%d %s" % (i+1, sp.doc.URL, sp.start_line, state)
      return False
      
    @inputcmd(_breakpointcmds, ConsoleKey.E)
    def _bp_enable(self, keyinfo):
      try:
        bp_num = int(Console.ReadLine())
        for i, bp in enumerate(self.breakpoints): 
          if i+1 == bp_num:
            bp.Activate(True)
            print "\nBreakpoint %d Enabled" % bp_num
            return False
        raise Exception, "Breakpoint %d not found" % bp_num
        
      except Exception, msg:
        with CC.Red: print "Enable breakpoint Failed", msg
      return False      
      
    @inputcmd(_breakpointcmds, ConsoleKey.D)
    def _bp_disable(self, keyinfo):
      try:
        bp_num = int(Console.ReadLine())
        for i, bp in enumerate(self.breakpoints): 
          if i+1 == bp_num:
            bp.Activate(False)
            print "\nBreakpoint %d Disabled" % bp_num
            return False
        raise Exception, "Breakpoint %d not found" % bp_num
        
      except Exception, msg:
        with CC.Red: print "Disable breakpoint Failed", msg
      return False      
      
    @inputcmd(_inputcmds, ConsoleKey.B)
    def _input_breakpoint(self, keyinfo):
        keyinfo2 = Console.ReadKey()
        if keyinfo2.Key in IPyDebugProcess._breakpointcmds:
            return IPyDebugProcess._breakpointcmds[keyinfo2.Key](self, keyinfo2)
        else:
            print "\nInvalid breakpoint command", str(keyinfo2.Key)
            return False
            
    def _input(self):
        offset, sp = get_frame_location(self.active_thread.ActiveFrame)
        lines = self._get_file(sp.doc.URL)
        self._print_source_line(sp, lines)

        while True:
            print "ipydbg» ",
            keyinfo = Console.ReadKey()
            if keyinfo.Key in IPyDebugProcess._inputcmds:
              if IPyDebugProcess._inputcmds[keyinfo.Key](self, keyinfo):
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
        if self.initial_breakpoint == None:
            self.initial_breakpoint = create_breakpoint(e.Module, self.py_file, 1)
            if self.initial_breakpoint != None:
              self.initial_breakpoint.Activate(True)
              self.breakpoints.append(self.initial_breakpoint)

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


