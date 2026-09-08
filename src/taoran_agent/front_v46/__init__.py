"""V4.6 frontend-only policy; backend scoring uses the current reviewer."""
POLICY_VERSION = "front-v46-complete-20260908"

def bind(reviewer):
    from .reviewer import FrontReviewer
    view = object.__new__(FrontReviewer)
    view.__dict__ = reviewer.__dict__.copy()
    return view
