from System import Console as _Console

class ConsoleColorMgr(object):
  def __init__(self, foreground = None, background = None):
    self.foreground = foreground
    self.background = background

  def __enter__(self):  
    self._tempFG = _Console.ForegroundColor  
    self._tempBG = _Console.BackgroundColor
    
    if self.foreground: _Console.ForegroundColor = self.foreground  
    if self.background: _Console.BackgroundColor = self.background
      
  def __exit__(self, t, v, tr):  
    _Console.ForegroundColor = self._tempFG 
    _Console.BackgroundColor = self._tempBG 

import sys    
_curmodule = sys.modules[__name__]

from System import ConsoleColor, Enum
for n in Enum.GetNames(ConsoleColor):
  setattr(_curmodule, n, ConsoleColorMgr(Enum.Parse(ConsoleColor, n)))
  
del ConsoleColor
del Enum
del sys
del _curmodule
del n