git filter-branch --force --index-filter "git rm --cached --ignore-unmatch license/device.wvd image.png" --prune-empty --tag-name-filter cat -- --all
