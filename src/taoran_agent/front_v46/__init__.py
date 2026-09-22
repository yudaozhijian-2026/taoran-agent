"""V4.6 frontend-only policy; backend scoring uses the current reviewer."""
POLICY_VERSION = "front-v46-goal-boundary-v14-20260922"

def bind(reviewer):
    from .reviewer import FrontReviewer
    view = object.__new__(FrontReviewer)
    view.__dict__ = reviewer.__dict__.copy()
    return view
