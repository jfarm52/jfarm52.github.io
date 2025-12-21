# Replit ↔ GitHub Sync Setup Guide

This guide explains how to keep your Replit project synced with this GitHub repository.

## 🎯 Two Sync Options

### Option 1: Manual Push from Replit (Recommended)
Push changes from Replit to GitHub whenever you're ready.

### Option 2: Automatic Sync from GitHub
GitHub Actions automatically fetches from Replit every 6 hours.

---

## 📤 Option 1: Setup Git in Replit

### One-Time Setup in Replit Shell:

```bash
# 1. Initialize git in your Replit project (if not already done)
git init

# 2. Add this GitHub repository as remote
git remote add github https://github.com/jfarm52/jfarm52.github.io.git

# 3. Configure git user (one time only)
git config user.name "Your Name"
git config user.email "your-email@example.com"
```

### 🔐 Authentication Setup:

**Using Personal Access Token (Recommended):**

1. Go to GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Click "Generate new token (classic)"
3. Give it a name like "Replit Sync"
4. Check the `repo` scope
5. Generate and **copy the token**
6. In Replit, add to Secrets (🔒 icon in sidebar):
   - Key: `GITHUB_TOKEN`
   - Value: Your personal access token

### 📤 Push Changes to GitHub:

**Simple version** (creates new commit each time):
```bash
# In Replit Shell:
git add index.html
git commit -m "Update from Replit - $(date)"
git push github main
```

**Create an alias for easy syncing:**
```bash
# Add this to your Replit shell:
alias sync-github='git add index.html && git commit -m "Update from Replit - $(date)" && git push github main'

# Now you can just run:
sync-github
```

### 🔄 Alternative: Using Replit's GitHub Integration

1. In Replit, click the **Git** icon in the left sidebar
2. Click **Connect to GitHub**
3. Authorize Replit to access your GitHub account
4. Link to repository: `jfarm52/jfarm52.github.io`
5. Now you can commit and push directly from Replit's UI!

---

## 🤖 Option 2: Automatic Sync via GitHub Actions

GitHub Actions is already set up to automatically fetch your latest code from `https://sitewalk.carlislenergy.com/` every 6 hours.

### Manual Trigger:
1. Go to: https://github.com/jfarm52/jfarm52.github.io/actions
2. Click on "Sync from Replit" workflow
3. Click "Run workflow"
4. Select branch (main) and run

### Disable Automatic Schedule:
If you don't want automatic syncing, edit `.github/workflows/sync-from-replit.yml` and remove the `schedule:` section.

---

## 🎬 Quick Start (Easiest Method)

**In Replit Shell, run these commands:**

```bash
# One-time setup
git init
git remote add github https://github.com/jfarm52/jfarm52.github.io.git
git config user.name "Justin"
git config user.email "your-email@example.com"

# Every time you want to sync to GitHub:
git add index.html
git commit -m "Update from Replit"
git push github main
```

**On first push**, GitHub will ask for authentication:
- Username: your GitHub username
- Password: Use your **Personal Access Token** (not your actual password)

---

## 📋 Best Practices

✅ **DO:**
- Commit meaningful changes (not every single keystroke)
- Write descriptive commit messages
- Test your app before pushing

❌ **DON'T:**
- Commit API keys or secrets
- Push broken code to main branch
- Overwrite GitHub changes without checking first

---

## 🆘 Troubleshooting

**Problem: "Authentication failed"**
- Solution: Use a Personal Access Token, not your password

**Problem: "Updates were rejected"**
- Solution: Pull first: `git pull github main --rebase` then push

**Problem: "Not a git repository"**
- Solution: Run `git init` in your Replit project folder

---

## 📞 Need Help?

- GitHub Docs: https://docs.github.com/en/get-started/getting-started-with-git
- Replit Docs: https://docs.replit.com/programming-ide/using-git-on-replit
