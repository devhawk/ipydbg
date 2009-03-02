import clr
clr.AddReference('CorDebug')

import sys

from System import Array
from System.IO import Path
from System.Reflection import Assembly
from System.Threading import AutoResetEvent
from System.Diagnostics.SymbolStore import ISymbolDocument, SymbolToken

from Microsoft.Samples.Debugging.CorDebug import CorDebugger
from Microsoft.Samples.Debugging.CorMetadata.NativeApi import IMetadataImport
from Microsoft.Samples.Debugging.CorSymbolStore import SymbolBinder

ipy = Assembly.GetEntryAssembly().Location
py_file = sys.argv[1]
cmd_line = "\"%s\" -D \"%s\"" % (ipy, py_file)

evt = AutoResetEvent(False)
sym_binder = SymbolBinder()
initial_breakpoint = None
  
class sequence_point(object):
  def __init__(self, offset, doc, start_line, start_col, end_line, end_col):
    self.offset = offset
    self.doc = doc
    self.start_line = start_line
    self.start_col = start_col
    self.end_line = end_line
    self.end_col = end_col
    
def get_sequence_points(method):
  spOffsets    = Array.CreateInstance(int, method.SequencePointCount)
  spDocs       = Array.CreateInstance(ISymbolDocument, method.SequencePointCount)
  spStartLines = Array.CreateInstance(int, method.SequencePointCount)
  spEndLines   = Array.CreateInstance(int, method.SequencePointCount)
  spStartCol   = Array.CreateInstance(int, method.SequencePointCount)
  spEndCol     = Array.CreateInstance(int, method.SequencePointCount)
  
  method.GetSequencePoints(spOffsets, spDocs, spStartLines, spStartCol, spEndLines, spEndCol)

  for i in range(method.SequencePointCount):
    yield sequence_point(spOffsets[i], spDocs[i], spStartLines[i], spStartCol[i], spEndLines[i], spEndCol[i])

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
  evt.Set()

def OnUpdateModuleSymbols(s,e):
  print "OnUpdateModuleSymbols"
  
  metadata_import = e.Module.GetMetaDataInterface[IMetadataImport]()
  reader = sym_binder.GetReaderFromStream(metadata_import, e.Stream)
 
  global initial_breakpoint
  if initial_breakpoint != None:
    return
    
  full_path = Path.GetFullPath(py_file)
  for doc in reader.GetDocuments():
    if str.IsNullOrEmpty(doc.URL):
      continue
    if str.Compare(full_path, Path.GetFullPath(doc.URL), True) == 0:
      initial_breakpoint = create_breakpoint(doc, 1, e.Module, reader)

debugger = CorDebugger(CorDebugger.GetDefaultDebuggerVersion())
process = debugger.CreateProcess(ipy, cmd_line)

process.OnCreateAppDomain += OnCreateAppDomain
process.OnProcessExit += OnProcessExit
process.OnUpdateModuleSymbols += OnUpdateModuleSymbols

process.Continue(False)

evt.WaitOne()