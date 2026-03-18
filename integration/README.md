# Real-World Integration Scenarios

This directory contains end-to-end integration tests and reproduction scripts for user-reported issues. Unlike the unit tests in `tests/`, these are intended to be run against a **live Colab environment**.

## **Prerequisites**
- A valid Google account with Colab access.
- `uv` installed locally.
- Authenticated state (run `colab sessions` to verify you can talk to the backend).

## **Scenarios**

### **1. Plot Redirection (`repro_plot_redirection/`)**
Tests the ability to execute a matplotlib script and redirect the intercepted plot to a specific local file.
- **Source**: User feedback regarding "Implicit Plot Handling".
- **Verified in**: v0.1.2

## **How to add a new scenario**
1. Create a sub-directory `repro_<issue_description>`.
2. Include a script (Python or Shell) that demonstrates the issue or verifies the fix.
3. Add a brief entry to this README.
