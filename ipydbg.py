import clr
clr.AddReference('CorDebug')

import sys
from System.Reflection import Assembly
from System.Threading import AutoResetEvent
from Microsoft.Samples.Debugging.CorDebug import CorDebugger

ipy = Assembly.GetEntryAssembly().Location
py_file = sys.argv[1]
cmd_line = "\"%s\" -D \"%s\"" % (ipy, py_file)

evt = AutoResetEvent(False)

def OnCreateAppDomain(s,e):
  print "OnCreateAppDomain", e.AppDomain.Name
  e.AppDomain.Attach()
  
def OnProcessExit(s,e):
  print "OnProcessExit"
  evt.Set()
  
debugger = CorDebugger(CorDebugger.GetDefaultDebuggerVersion())
process = debugger.CreateProcess(ipy, cmd_line)

process.OnCreateAppDomain += OnCreateAppDomain
process.OnProcessExit += OnProcessExit

process.Continue(False)

evt.WaitOne()