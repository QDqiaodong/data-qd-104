"""Currency-aware integer minor units; no floating-point money."""
import re

SCALE={"USD":2,"EUR":2,"JPY":0}


def minor(text,currency,signed=False):
    if currency not in SCALE or not isinstance(text,str):raise ValueError("unsupported currency or non-string amount")
    if not re.fullmatch(r"-?\d{1,12}(?:\.\d{1,2})?",text):raise ValueError("invalid decimal amount")
    negative=text.startswith("-")
    if negative and not signed:raise ValueError("ledger amounts must be positive")
    whole,dot,fraction=text.lstrip("-").partition(".")
    scale=SCALE[currency]
    if len(fraction)>scale:raise ValueError("excess currency precision")
    value=int(whole)*10**scale+int((fraction+"0"*scale)[:scale] or "0")
    if value>10**12 or (not signed and value==0):raise ValueError("amount outside minor-unit bounds")
    return -value if negative else value
