import clr
clr.AddReference('CorDebug')

import sys

from System import Array, Console, ConsoleKey
from System.IO import Path
from System.Reflection import Assembly
from System.Threading import WaitHandle, AutoResetEvent
from System.Diagnostics.SymbolStore import ISymbolDocument, SymbolToken

from Microsoft.Samples.Debugging.CorDebug import CorDebugger, CorFrameType
from Microsoft.Samples.Debugging.CorMetadata import CorMetadataImport
from Microsoft.Samples.Debugging.CorMetadata.NativeApi import IMetadataImport
from Microsoft.Samples.Debugging.CorSymbolStore import SymbolBinder

#use the current executing version of IPY to launch the debug process
ipy = Assembly.GetEntryAssembly().Location
py_file = sys.argv[1]
cmd_line = "\"%s\" -D \"%s\"" % (ipy, py_file)

terminate_event = AutoResetEvent(False)
break_event = AutoResetEvent(False)

sym_binder = SymbolBinder()
initial_breakpoint = None
symbol_readers = dict()

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
  spOffsets    = Array.CreateInstance(int, method.SequencePointCount)
  spDocs = Array.CreateInstance(ISymbolDocument, method.SequencePointCount)
  spStartLines = Array.CreateInstance(int, method.SequencePointCount)
  spEndLines   = Array.CreateInstance(int, method.SequencePointCount)
  spStartCol   = Array.CreateInstance(int, method.SequencePointCount)
  spEndCol     = Array.CreateInstance(int, method.SequencePointCount)
  
  method.GetSequencePoints(spOffsets, spDocs, spStartLines, spStartCol, 
                           spEndLines, spEndCol)

  for i in range(method.SequencePointCount):
    yield sequence_point(spOffsets[i], spDocs[i], spStartLines[i], 
                         spStartCol[i], spEndLines[i], spEndCol[i])

def get_location(frame):
  offset, mapping_result = frame.GetIP()
  
  if frame.FrameType != CorFrameType.ILFrame:
    return offset, None
  if frame.Function.Module not in symbol_readers:
    return offset, None
    
  reader = symbol_readers[frame.Function.Module]
  method = reader.GetMethod(SymbolToken(frame.Function.Token))
  
  real_sp = None
  for sp in get_sequence_points(method):
    if sp.offset > offset: 
      break
    if sp.start_line != 0xfeefee: 
      real_sp = sp
      
  if real_sp == None:
    return offset, None
  
  return offset, real_sp
  
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

def OnCreateAppDomain(s,e):
  print "OnCreateAppDomain", e.AppDomain.Name
  e.AppDomain.Attach()
  
def OnProcessExit(s,e):
  print "OnProcessExit"
  terminate_event.Set()

def OnUpdateModuleSymbols(s,e):
  print "OnUpdateModuleSymbols"
  
  metadata_import = e.Module.GetMetaDataInterface[IMetadataImport]()
  reader = sym_binder.GetReaderFromStream(metadata_import, e.Stream)
 
  global symbol_readers
  symbol_readers[e.Module] = reader
  
  global initial_breakpoint
  if initial_breakpoint != None:
    return
    
  full_path = Path.GetFullPath(py_file)
  for doc in reader.GetDocuments():
    if str.IsNullOrEmpty(doc.URL):
      continue
    if str.Compare(full_path, Path.GetFullPath(doc.URL), True) == 0:
      initial_breakpoint = create_breakpoint(doc, 1, e.Module, reader)

def OnBreakpoint(s,e):
  func = e.Thread.ActiveFrame.Function
  metadata_import = CorMetadataImport(func.Module)
  method_info = metadata_import.GetMethodInfo(func.Token)

  offset, sp = get_location(e.Thread.ActiveFrame)
  print "OnBreakpoint", method_info.Name, "Location:", sp if sp != None else "offset %d" % offset
  do_break_event(e)

def OnStepComplete(s,e):
  offset, sp = get_location(e.Thread.ActiveFrame)
  print "OnStepComplete Reason:", e.StepReason, "Location:", sp if sp != None else "offset %d" % offset
  do_break_event(e)
  
def do_break_event(e):
  global active_thread
  active_thread = e.Thread
  e.Continue = False
  break_event.Set()
  
def get_dynamic_frames(chain):
  for f in chain.Frames:
    if f.FrameType != CorFrameType.ILFrame:
      continue
    metadata_import = CorMetadataImport(f.Function.Module)
    method_info = metadata_import.GetMethodInfo(f.FunctionToken)
    typename = method_info.DeclaringType.Name
    if typename.startswith("Microsoft.Scripting.") \
      or typename.startswith("IronPython.") \
      or typename == "PythonConsoleHost":
        continue
    yield f
    
def input():
  while True:
    Console.Write("» ")
    k = Console.ReadKey()
    
    if k.Key == ConsoleKey.Spacebar:
      print "\nContinuing"
      return
    elif k.Key == ConsoleKey.Q:
      print "\nQuitting"
      process.Stop(0)
      process.Terminate(255)
      return
    elif k.Key == ConsoleKey.T:
      print "\nStack Trace"
      for f in get_dynamic_frames(active_thread.ActiveChain):
        offset, sp = get_location(f)
        metadata_import = CorMetadataImport(f.Function.Module)
        method_info = metadata_import.GetMethodInfo(f.FunctionToken)
        print "  ", \
          "%s::%s --" % (method_info.DeclaringType.Name, method_info.Name), \
          sp if sp != None else "(offset %d)" % offset
    else:
      print "\nPlease enter a valid command"

debugger = CorDebugger(CorDebugger.GetDefaultDebuggerVersion())
process = debugger.CreateProcess(ipy, cmd_line)

process.OnCreateAppDomain += OnCreateAppDomain
process.OnProcessExit += OnProcessExit
process.OnUpdateModuleSymbols += OnUpdateModuleSymbols
process.OnBreakpoint += OnBreakpoint
process.OnStepComplete += OnStepComplete

handles = Array.CreateInstance(WaitHandle, 2)
handles[0] = terminate_event
handles[1] = break_event

while True:
  process.Continue(False)

  i = WaitHandle.WaitAny(handles)
  if i == 0:
    break

  input()
  

