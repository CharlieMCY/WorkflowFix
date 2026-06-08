"""backport_ir — frame the workflow-fix task as backporting.

Compile a master clean-fix commit's diff into an executable, drift-tolerant
backport-patch IR, replay it onto a release branch, and verify the result.

Light imports only (compile + match + IR types); `apply` and `verify` are
imported on demand because they pull in ruamel.yaml / zizmor respectively.
"""
from .compile import compile_program
from .ir import Anchor, Edit, IRProgram, Pin, Seg
from .match import AnchorMatch, resolve

__all__ = [
    "IRProgram",
    "Edit",
    "Anchor",
    "Seg",
    "Pin",
    "compile_program",
    "resolve",
    "AnchorMatch",
]
