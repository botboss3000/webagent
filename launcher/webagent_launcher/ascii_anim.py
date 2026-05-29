"""Animation style constants.

The actual rendering happens in stage.py (AnimatedStage widget), which
combines the animated field with the logo overlay in a single render pass.
This module just defines the style identifiers everything else imports.
"""

PLASMA = "plasma"
FLOWFIELD = "flowfield"
RINGS = "rings"
STATIC = "static"
ANIM_STYLES = (PLASMA, FLOWFIELD, RINGS, STATIC)
