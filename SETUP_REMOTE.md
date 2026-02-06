# Finish remote setup

Repo is initialized with `origin` = `git@github.com:Phasm22/llmLibrarian.git`.

1. **Create the GitHub repo** (if you haven’t):
   - Go to https://github.com/new
   - Repository name: `llmLibrarian`
   - Visibility: **Private**
   - Do **not** add a README, .gitignore, or license (we already have them)
   - Create repository

2. **If your GitHub username is not Phasm22**, update the remote:
   ```bash
   git remote set-url origin git@github.com:YOUR_USERNAME/llmLibrarian.git
   ```

3. **Fix SSH / auth if push failed**:
   - Test: `ssh -T git@github.com`
   - If “Permission denied”, add your key: `ssh-add -l` then `ssh-add ~/.ssh/your_key`
   - Or re-auth with GitHub CLI: `gh auth login -h github.com`

4. **Push**:
   ```bash
   git push -u origin main
   ```
