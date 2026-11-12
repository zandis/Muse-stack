"""Health-management bot: a delegate that works on the user's own records.

Built to the sensitivity of medical data: records are encrypted with a key
only the user holds, every bot action needs a mandate the user granted, every
action leaves a signed receipt, and health content is only ever sent to models
the tenant has marked as on-premises ("local") or contractually private.
Data exchange for research is an add-on, off by default.
"""
