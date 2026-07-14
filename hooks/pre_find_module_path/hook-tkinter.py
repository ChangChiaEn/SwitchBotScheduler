def pre_find_module_path(hook_api):
    # The local Python install can fail PyInstaller's Tcl/Tk probe even when
    # tkinter itself is present. We collect Tcl/Tk explicitly in build.bat.
    return
