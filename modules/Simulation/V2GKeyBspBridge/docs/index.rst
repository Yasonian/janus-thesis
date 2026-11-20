.. _everest_modules_handwritten_V2GKeyBspBridge:

..  This file is a placeholder for optional multiple files
    handwritten documentation for the V2GKeyBspBridge module.
    
..  This handwritten documentation is optional. In case
    you do not want to write it, you can delete the doc/ directory.

..  The documentation can be written in reStructuredText,
    and will be converted to HTML and PDF by Sphinx.
    This index.rst file is the entry point for the module documentation.

..  Use underlined-only headlines inside this document (highest-level
    sub-section headline should use "=" characters)

..  The content of this file will be included in the auto-generated HTML
    page for the module. You can link to it using the following
    reference: everest_modules_V2GKeyBspBridge.

.. *******************************************
.. V2GKeyBspBridge
.. *******************************************

SiL bridge that sits between a real evse_board_support provider (YetiSimulator) and an EvseManager, forwarding all commands/vars transparently EXCEPT that, during a VirtualV2GKey power-path switch, it OVERRIDES the upstream CP-state `event` feed with BspEvents derived from v2g_key_control.cp_state. This lets VirtualV2GKey (JANUS) actually drive the manager's IEC state machine without patching EVerest core. Pure test glue; models no real hardware.
