#!/bin/bash
# Quick sync script for Replit → GitHub
# Usage: bash sync-to-github.sh "Your commit message"

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}🔄 Syncing to GitHub...${NC}"

# Check if git is initialized
if [ ! -d .git ]; then
    echo -e "${RED}❌ Git not initialized. Run setup first:${NC}"
    echo "   git init"
    echo "   git remote add github https://github.com/jfarm52/jfarm52.github.io.git"
    exit 1
fi

# Check if remote exists
if ! git remote | grep -q "github"; then
    echo -e "${BLUE}Adding GitHub remote...${NC}"
    git remote add github https://github.com/jfarm52/jfarm52.github.io.git
fi

# Get commit message from argument or use default
COMMIT_MSG="${1:-Update from Replit - $(date '+%Y-%m-%d %H:%M:%S')}"

# Add files
echo -e "${BLUE}📦 Adding files...${NC}"
git add index.html

# Check if there are changes to commit
if git diff --staged --quiet; then
    echo -e "${GREEN}✓ No changes to commit${NC}"
    exit 0
fi

# Commit
echo -e "${BLUE}💾 Committing changes...${NC}"
git commit -m "$COMMIT_MSG"

# Push to GitHub
echo -e "${BLUE}⬆️  Pushing to GitHub...${NC}"
git push github main || git push github master

echo -e "${GREEN}✓ Successfully synced to GitHub!${NC}"
echo -e "${GREEN}View at: https://github.com/jfarm52/jfarm52.github.io${NC}"
