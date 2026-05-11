with open("index.html", "r", encoding="utf-8") as f:
    html = f.read()

settings_html = """
    </div>

    <!-- DB UI Settings -->
    <div style="margin-bottom:20px;padding:16px;background:#0d0d1a;border-radius:8px;border:1px solid #2a2a4a;">
      <label style="display:block;margin-bottom:12px;color:#a9b1d6;font-size:13px;font-weight:600;">Display Settings</label>
      <div style="display:flex;align-items:center;gap:10px;">
        <input type="checkbox" id="db-setting-show-hidden" style="cursor:pointer;width:16px;height:16px;accent-color:#7dcfff;">
        <label for="db-setting-show-hidden" style="cursor:pointer;color:#c0caf5;font-size:13px;">Show Hidden Columns</label>
      </div>
      <div style="margin-top:6px;font-size:11px;color:#565f89;">
        When enabled, you can toggle visibility of individual columns in the data view using the eye icon.
      </div>
    </div>

    <div style="padding:12px;background:#0d0d1a;border-radius:8px;border-left:3px solid #7dcfff;font-size:12px;color:#a9b1d6;">
"""

old_html = """
    </div>

    <div style="padding:12px;background:#0d0d1a;border-radius:8px;border-left:3px solid #7dcfff;font-size:12px;color:#a9b1d6;">
"""

new_index = html.replace(old_html, settings_html)
with open("index.html", "w", encoding="utf-8") as f:
    f.write(new_index)
print("done")
