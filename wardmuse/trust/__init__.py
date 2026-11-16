"""Trust layer: encrypted vault, mandates and receipts.

Records are encrypted with a key only the user holds, every bot action needs a
mandate the user granted, and every action leaves a signed receipt. Packs such
as the health assistant build on this.
"""
