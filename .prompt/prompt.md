### Genesis

Can you write a python application (having UIs) 
sucht that will merge multiple .venv 
(determine dependencies, determine python versions) into a single unified .venv file?

It allows the user to select multiple folders 

+---------------+
|  (Add folders)|
|               |
|  folders:     |
|   folder 1    |
|   folder 2    |
|               |
|       (merge) |
+---------------+

When user click on merge, it will scan for the dependencies and python versions and it will allow user to select targeted location to have the environment saved. 

It will install the python and environment for the user.

If there's any conflict among the versions of the dependencies, it will a screen that has a list of version to let the user to select:


+---------------------+
|  detected conflicts |
|                     |
|   python [3.1.0  v] |
|   torch  [cuda   v] |
|   numpy  [1.0.0  v] |
+---------------------+


Note v means the dropdown.

Similar to what we have in cygwin. By default, the order of these versions should always be the highest first.