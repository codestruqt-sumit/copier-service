"""Automated Test Suite (ATS) for the Signal Provider -> Copier flow.

The ATS ONLY generates signals through the Signal Provider API and OBSERVES what happens
downstream (copier machine logs + diagnostic status). It never instructs the copier or
the trading terminal to execute anything, never calls terminal functions, and never
inspects the terminal directly.
"""
