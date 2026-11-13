"""Keep scheduled pythonw jobs console-free without hiding task exit codes."""
import os
import sys


def prepare_stdio():
    # pythonw has no standard streams. Existing JSON state files remain the logs.
    # Keep valid file handles for argparse, print and child-process inheritance.
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))
