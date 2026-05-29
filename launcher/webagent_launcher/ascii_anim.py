"""Animation style constants.

The actual rendering happens in stage.py (AnimatedStage widget), which
combines the animated field with the logo overlay in a single render pass.
This module just defines the style identifiers everything else imports.
"""

PLASMA = "plasma"
FLOWFIELD = "flowfield"
RINGS = "rings"
STATIC = "static"   # scrolling TV-noise field (labelled "Noise" in the UI)
OFF = "off"         # true idle — no motion, no frame timer, ~0% CPU

# Order used by the Space-cycle and the style picker. "off" lives last so
# cycling lands on it after the motion styles.
ANIM_STYLES = (PLASMA, FLOWFIELD, RINGS, STATIC, OFF)

# Human-friendly labels for the settings picker. Keeps the internal value
# `static` (so saved configs still load) while showing "Noise", and surfaces
# the new "Off" choice clearly.
ANIM_LABELS = {
    PLASMA: "Plasma",
    FLOWFIELD: "Flow Field",
    RINGS: "Rings",
    STATIC: "Noise",
    OFF: "Off",
}
