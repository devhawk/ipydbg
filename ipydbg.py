import clr
clr.AddReference('CorDebug')

import sys

from System.Reflection import Assembly
from System.Threading import AutoResetEvent

from Microsoft.Samples.Debugging.CorDebug import CorDebugger
from Microsoft.Samples.Debugging.CorMetadata.NativeApi import IMetadataImport
from Microsoft.Samples.Debugging.CorSymbolStore import SymbolBinder

ipy = Assembly.GetEntryAssembly().Location
py_file = sys.argv[1]
cmd_line = "\"%s\" -D \"%s\"" % (ipy, py_file)

evt = AutoResetEvent(False)
symBinder = SymbolBinder()
  
def OnCreateAppDomain(s,e):
  print "OnCreateAppDomain", e.AppDomain.Name
  e.AppDomain.Attach()
  
def OnProcessExit(s,e):
  print "OnProcessExit"
  evt.Set()

def OnUpdateModuleSymbols(s,e):
  print "OnUpdateModuleSymbols"
  
  metadata_import = e.Module.GetMetaDataInterface[IMetadataImport]()
  reader = symBinder.GetReaderFromStream(metadata_import, e.Stream)

  for doc in reader.GetDocuments():
    print "\t", doc.URL

debugger = CorDebugger(CorDebugger.GetDefaultDebuggerVersion())
process = debugger.CreateProcess(ipy, cmd_line)

process.OnCreateAppDomain += OnCreateAppDomain
process.OnProcessExit += OnProcessExit
process.OnUpdateModuleSymbols += OnUpdateModuleSymbols

process.Continue(False)

evt.WaitOne()