# Computer Go Server

## Python Server

python3 -m cgos.app.cgos cgos19.ini

## Original Tcl Server

How to build from source:
1. Add tclkit to your path
  a. Identify your platform specific tclkit in third_party directory
  b. Make a copy (called tclkit) somewhere in your path
     (~/bin and /usr/bin are good candidates for unix systems)
2. Go into the directory of the component you wish to build (e.g. client)
3. Run gnu make (on unix systems, this should just be 'make')

Is there no tclkit for your environment in the repository?
Other kits can be downloaded from  http://www.equi4.com

Don't have gnu make?  You have two options:
Option 1: Obtain gnu make
Option 2: Run individual commands out of the makefile (e.g. sdx wrap)
