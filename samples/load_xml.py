from System import IO

def print_item(i):
  title = i.SelectSingleNode('title/text()').Value
  pubDate = i.SelectSingleNode('pubDate/text()').Value
  print title, pubDate

import clr
clr.AddReference('System.Xml')

from System.Xml import XmlDocument
from System import DateTime

_curdir = IO.Path.GetDirectoryName(__file__)
_xmlfile = IO.Path.Combine(_curdir, "Devhawk.RSS.xml")

xml = XmlDocument()
xml.Load(_xmlfile)

items = xml.SelectNodes('/rss/channel/item')

for i in items:
  print_item(i)