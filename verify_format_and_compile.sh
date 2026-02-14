#!/bin/bash
# CI-friendly verification script for formatting and compilation
# Exits with non-zero code if any check fails

set -e

echo "============================================"
echo "Format and Compilation Verification"
echo "============================================"
echo ""

# Check 1: Compile all Python files
echo "✓ Step 1: Compiling all Python files..."
python -m compileall src/ tests/ -q
echo "  ✓ All files compile successfully"
echo ""

# Check 2: Check Black formatting
echo "✓ Step 2: Checking Black formatting..."
if python -m black src/ tests/ --check --line-length 88 --quiet 2>&1; then
    echo "  ✓ All files are properly formatted"
else
    echo "  ✗ Files need formatting. Run: python -m black src/ tests/ --line-length 88"
    exit 1
fi
echo ""

echo "============================================"
echo "✅ All checks passed!"
echo "============================================"
