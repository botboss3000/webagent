// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║  SISTER-PANEL: AGENT-ABILITY-TABLE  —  KEEP THE TWO IN SYNC                 ║
// ║  This is the **agent** ability table (per-agent controls).                   ║
// ║  The **admin** ability table is `admin-ability-table.js`.                    ║
// ║  Both render the shared `.ac-list` / `.ac-row` / `.ac-tri` component and     ║
// ║  read as the SAME table. If you change the look, structure, grouping, or     ║
// ║  toggle behaviour here, apply the matching change in admin-ability-table.js. ║
// ║  (grep `SISTER-PANEL: AGENT-ABILITY-TABLE`).                                 ║
// ║                                                                              ║
// ║  The agent table is the SUBSET — it mirrors the admin table's visual         ║
// ║  structure but omits admin-only features. Never add a feature here that      ║
// ║  the admin table does not already have.                                      ║
// ╚══════════════════════════════════════════════════════════════════════════════╝
//
// Dependencies: `ABILITY_TO_TOOLS` / `_CONN_ICONS` are imported DIRECTLY from
// the agents/state.js ES module (they are NOT window globals — reading window.*
// silently yields empty data, which is what broke this panel after the refactor).

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

import {
  _esc, _iconHtml, _chevronSvg,
  _buildToolRow, _buildAbilitySettingsList, _buildEyeToggle,
  _buildMoreButton, _buildGroupStatusCard, _paintGroupStatus,
  _buildAbilityTreeLoader, _staggerRevealGroups, _buildSettingsRowsHtml,
  buildCredentialsSection, _flashStatusText, _markSaving, _flashSaveCheck,
  buildAbilitySearchPill, _buildGroupTriSpinner, _buildGroupBodyLoading,
  _refreshLucideIcons, loadAbilityCatalog, invalidateAbilityCatalog,
} from './dom-utils.js';
import { spawnWebagentAbilityChat } from '../../chat-widget/js/chat-widget.js';
import { ABILITY_TO_TOOLS, _CONN_ICONS } from '../../main-panel/agents/js/state.js';
import { authHeaders } from './left-login.js';

// ── Catalog fetch ─────────────────────────────────────────────────────────────
// Loading + caching is shared with admin-ability-table.js via loadAbilityCatalog()
// / invalidateAbilityCatalog() in dom-utils.js (both panels previously kept a
// byte-identical copy of the loader + cache).

// ── Fallback data (mirror of admin-ability-table.js — keep identical) ─────────

// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  SHARED VISUAL SKELETON — mirror in admin-ability-table.js              ║
// ║  If you change any of these functions, mirror the change there.         ║
// ║  The CSS classes they use live in app3.css.                             ║
// ╚══════════════════════════════════════════════════════════════════════════╝

// Imported from shared/dom-utils.js: _esc, _iconHtml, _chevronSvg

function _buildGroup(groupId, name, icon, color, desc) {
  const groupEl = document.createElement('div');
  groupEl.className = 'ac-row ac-group';
  groupEl.id = 'ac-group-' + groupId;

  const head = _buildGroupHead(groupId, name, icon, color, desc);
  const bodyEl = document.createElement('div');
  bodyEl.className = 'ac-group-body';
  bodyEl.id = 'ac-group-body-' + groupId;

  groupEl.appendChild(head);
  groupEl.appendChild(bodyEl);

  head.addEventListener('click', e => {
    if (e.target.closest('.ac-tri, button')) return;
    groupEl.classList.toggle('expanded');
  });

  return groupEl;
}

function _buildGroupHead(groupId, name, icon, color, desc) {
  const head = document.createElement('div');
  head.className = 'ac-ability-row ac-group-head';

  // Expand chevron — LEFT of the icon (mirrors admin-ability-table.js).
  const chevron = document.createElement('span');
  chevron.className = 'ac-row-chevron ac-row-chevron--left';
  chevron.innerHTML = _chevronSvg();
  head.appendChild(chevron);

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = color;
  iconWrap.innerHTML = _iconHtml(icon, '18px');

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = `<div class="ac-ability-name"></div><div class="ac-ability-desc"></div>`;
  label.querySelector('.ac-ability-name').textContent = name;
  label.querySelector('.ac-ability-desc').textContent = desc;

  head.appendChild(iconWrap);
  head.appendChild(label);
  return head;
}

/** Append a read-only group status card to a group head (replaces the old
 *  interactive tri-toggle). State: 'off' → Disabled, 'mixed' → Partial,
 *  'on' → Enabled, 'na' → "—" (no togglable members). The card is display-only —
 *  bulk group toggling is intentionally gone. (SISTER-PANEL: AGENT-ABILITY-TABLE.) */
function _appendGroupStatus(head, statusId, state, title) {
  const status = _buildGroupStatusCard(statusId, state, title);
  head.appendChild(status);
  return status;
}

// Imported from shared/dom-utils.js: _buildTriToggle, _wireTriToggle

// (Dead code removed: _buildMemberRow, _appendToggle — these are admin-only
// and were never called from this file. They lived in the shared skeleton
// section but were only used by admin-ability-table.js.)
// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  END SHARED SKELETON                                                    ║
// ╚══════════════════════════════════════════════════════════════════════════╝

// ── Agent-specific: build a connection card (head + optional expandable body) ──

/**
 * Build one connection card for the agent table.
 * Returns a DOM element: either a plain `.ac-ability-row` (simple ability)
 * or an `.ac-row` wrapper with an expandable `.ac-ability-body`.
 */
function _buildConnectionCard(conn, { agent, canEdit, abilitiesByProvider, onSave, userId, agentToolsLoader }) {
  const isComingSoon = conn.status === 'coming_soon';
  const abilityId = conn.connection_type;
  const displayName = conn.display_name || abilityId;
  const desc = conn.description || '';
  // A locked-on safety ability (e.g. Context Control) is always enabled and its
  // toggle is fixed ON — clicking it can't turn it off, it just reports so.
  const lockedOn = !!conn.locked_on;
  const entitlementAllowed = conn.entitlement_allowed !== false;
  canEdit = Boolean(canEdit && entitlementAllowed);
  const enabled = entitlementAllowed && (lockedOn ? true : !!conn.enabled);
  // Every non-coming-soon row carries the plain on/off switch. Ability rows add
  // a ⋯ more button (visibility + caller access) instead of the old row-level
  // controls; OAuth / channel / credential cards keep just the switch (they have
  // no visibility mode or caller-access gate).

  // Wrapper. `ac-row` is required so the shared `.ac-row.expanded > .ac-ability-body`
  // rule (app3.css) reveals the body when the card is expanded — the card is the
  // direct parent of both the head (`.ac-ability-row`) and the body (`.ac-ability-body`).
  const card = document.createElement('div');
  card.className = 'conn-card ac-row' + (isComingSoon ? ' coming-soon' : '')
    + (enabled ? ' enabled' : '') + (entitlementAllowed ? '' : ' entitlement-locked');
  card.dataset.connType = abilityId;

  // ── Header row ──
  const header = document.createElement('div');
  header.className = 'conn-card-header ac-ability-row';

  header.innerHTML = `
    <span class="conn-icon ac-ability-icon">${_iconHtml(_getIcon(abilityId), '16px')}</span>
    <div class="ac-ability-label">
      <div class="conn-name ac-ability-name">${_esc(displayName)}</div>
      ${desc ? `<div class="ac-ability-desc">${_esc(desc)}</div>` : ''}
    </div>
    ${isComingSoon
      ? '<span class="conn-badge coming-soon">Coming soon</span>'
      : `${entitlementAllowed ? '' : '<span class="conn-badge">Upgrade required</span>'}<span class="ac-ability-status"></span>
         <label class="conn-toggle-wrap ac-ability-toggle-wrap${lockedOn ? ' ac-ability-toggle-locked' : ''}" title="${lockedOn ? 'Always on — safety device, cannot be deactivated' : (canEdit ? (enabled ? 'Disable' : 'Enable') : (enabled ? 'Enabled (admin only)' : 'Disabled (admin only)'))}">
           <input type="checkbox" class="conn-toggle" ${enabled ? 'checked' : ''} ${canEdit ? '' : 'disabled'}>
           <span class="conn-toggle-track"></span>
         </label>`}
  `;

  card.appendChild(header);

  if (isComingSoon) return card;

  // ── Expandable body (ability-type: config settings + tool permissions) ──
  // The body — tool rows, skill, credentials, settings, schema preview — is built
  // LAZILY the first time the card is expanded. Up front we render only the
  // top-level row (name + toggle + chevron + eye), so the initial list paints
  // without the agent's heavy resolved-tool set or per-ability DOM.
  if (conn.section === 'ability' && agent) {
    header.classList.add('conn-card-header-clickable');
    const chevron = document.createElement('span');
    chevron.className = 'ac-row-chevron ac-row-chevron--left';
    chevron.innerHTML = _chevronSvg();
    header.insertBefore(chevron, header.firstChild);

    // Holds the lazily-built body element once expanded (null until then). The
    // header eye toggle below guards on it so it can fire before first expand.
    let body = null;
    let _bodyPromise = null;
    const ensureBody = async () => {
      if (body) return body;
      if (!_bodyPromise) {
        _bodyPromise = _buildAbilityBody(conn, { agent, canEdit, abilitiesByProvider, onSave, userId, agentToolsLoader })
          .then(el => { body = el; if (el) card.appendChild(el); return el; });
      }
      return _bodyPromise;
    };

    // ── "More" (⋯) menu — the row's only control beyond the toggle. Visibility
    // (Off · Discoverable · Visible) and caller access (Everyone · Registered ·
    // Admins) moved OFF the row into the shared floating menu, so the title line
    // holds just the on/off switch (narrow-screen friendly). The menu items are
    // rebuilt on every open so they always reflect the live connection state.
    // The mode/access saves reuse the same endpoints + spinner → ✓ / ⚠ overlay
    // language the old row-level controls used.
    const _syncRowToggle = () => {
      const t = card.querySelector('.conn-toggle');
      if (t) t.checked = !!conn.enabled;
    };
    async function _setVisibility(next, btn) {
      const wantEnabled = next !== 'off';
      const wantMode = next === 'visible' ? 'visible' : 'discoverable';
      _markSaving(btn);
      try {
        // (1) Enable/disable if the on/off state actually changed.
        if (wantEnabled !== !!conn.enabled) {
          const config = _collectConfig(card, abilityId);
          await Promise.resolve(onSave(abilityId, wantEnabled, config));
          conn.enabled = wantEnabled;
          card.classList.toggle('enabled', wantEnabled);
          _syncRowToggle();
          // Let the parent group status card (Disabled · Partial · Enabled)
          // re-sync from the new state.
          card.dispatchEvent(new CustomEvent('ability-enabled-change', { bubbles: true }));
        }
        // (2) When on, ensure the visibility mode matches the chosen detent.
        if (wantEnabled && conn.ability_mode !== wantMode) {
          const res = await fetch(`/api/v1/agents/${agent.id}/abilities/${encodeURIComponent(abilityId)}/visibility`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ user_id: userId, mode: wantMode }),
          });
          if (!res.ok) throw new Error('save failed');
          conn.ability_mode = wantMode;
          const b = await ensureBody();
          if (b && typeof b._setVisInfo === 'function') b._setVisInfo('ability', wantMode);
          if (b && typeof b._refreshSchema === 'function') b._refreshSchema();
        }
        _flashSaveCheck(btn, true);
      } catch (_) {
        _flashSaveCheck(btn, false);
      }
    }
    async function _setAccess(level, btn) {
      _markSaving(btn);
      try {
        const res = await fetch(`/api/v1/agents/${agent.id}/abilities/${encodeURIComponent(abilityId)}/access`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ user_id: userId, level }),
        });
        if (!res.ok) throw new Error('save failed');
        conn.available_to = level;
        _flashSaveCheck(btn, true);
      } catch (_) {
        _flashSaveCheck(btn, false);
      }
    }
    const moreBtn = _buildMoreButton(() => {
      const mode = (conn.enabled && conn.ability_mode === 'discoverable') ? 'discoverable' : (conn.enabled ? 'visible' : 'off');
      const access = conn.available_to || 'everyone';
      const items = [
        // Visibility — the three detents of the old row-level tri, same semantics.
        { icon: 'eye-off', label: 'Off', checked: mode === 'off', disabled: !canEdit || lockedOn, action: () => _setVisibility('off', moreBtn) },
        { icon: 'download', label: 'Discoverable', checked: mode === 'discoverable', disabled: !canEdit, action: () => _setVisibility('discoverable', moreBtn) },
        { icon: 'eye', label: 'Visible', checked: mode === 'visible', disabled: !canEdit, action: () => _setVisibility('visible', moreBtn) },
        { separator: true },
      ];
      // "Available to" — caller-access gate (Everyone · Registered · Admins).
      for (const lvl of _ACCESS_LEVELS) {
        items.push({ icon: lvl.icon, label: lvl.label, checked: access === lvl.value, disabled: !canEdit, action: () => _setAccess(lvl.value, moreBtn) });
      }
      return items;
    }, { title: 'More options — visibility & who can trigger this ability' });
    header.appendChild(moreBtn);

    header.addEventListener('click', async e => {
      if (e.target.closest('.conn-toggle-wrap, .agents-eye-toggle, .ac-tri, .ac-access-wrap')) return;
      const willExpand = !card.classList.contains('expanded');
      if (willExpand) await ensureBody();
      card.classList.toggle('expanded');
      card.classList.toggle('conn-card-expanded');
    });
  }

  // ── Telegram body ──
  if (conn.connection_type === 'telegram') {
    const body = _buildTelegramBody(conn, { agent, canEdit, onSave });
    if (body) {
      card.appendChild(body);
      header.classList.add('conn-card-header-clickable');
      const chevron = document.createElement('span');
      chevron.className = 'ac-row-chevron ac-row-chevron--left';
      chevron.innerHTML = _chevronSvg();
      header.insertBefore(chevron, header.firstChild);
      header.addEventListener('click', e => {
        if (e.target.closest('.conn-toggle-wrap')) return;
        card.classList.toggle('expanded');
        card.classList.toggle('conn-card-expanded');
      });
    }
  }

  // ── OAuth body (google, microsoft, etc.) ──
  if (conn.section !== 'ability' && conn.connection_type !== 'telegram' && !isComingSoon) {
    const body = _buildOAuthBody(conn, { agent, canEdit, abilitiesByProvider, onSave });
    if (body) {
      card.appendChild(body);
      header.classList.add('conn-card-header-clickable');
      const chevron = document.createElement('span');
      chevron.className = 'ac-row-chevron ac-row-chevron--left';
      chevron.innerHTML = _chevronSvg();
      header.insertBefore(chevron, header.firstChild);
      header.addEventListener('click', e => {
        if (e.target.closest('.conn-toggle-wrap')) return;
        card.classList.toggle('expanded');
        card.classList.toggle('conn-card-expanded');
      });
    }
  }

  // ── Wire toggle ──
  const toggle = card.querySelector('.conn-toggle');
  if (toggle && canEdit) {
    toggle.addEventListener('change', async () => {
      // Safety device — fixed ON. Snap back and report instead of saving.
      if (lockedOn) {
        toggle.checked = true;
        _flashStatusText(header.querySelector('.ac-ability-status'), 'Cannot be deactivated', 'warn');
        return;
      }
      const config = _collectConfig(card, abilityId);
      // The spinner → ✓ / ⚠ confirm overlays ON TOP of the toggle while the
      // per-agent save runs. (SISTER-PANEL: AGENT-ABILITY-TABLE.)
      const wrap = toggle.closest('.ac-ability-toggle-wrap');
      _markSaving(wrap);
      toggle.disabled = true;
      try {
        await Promise.resolve(onSave(abilityId, toggle.checked, config));
        _flashSaveCheck(wrap, true);
        // Reflect the saved state on the card and let the parent group's status
        // card (Disabled · Partial · Enabled) re-sync from it.
        card.classList.toggle('enabled', toggle.checked);
        card.dispatchEvent(new CustomEvent('ability-enabled-change', { bubbles: true }));
      } catch (e) {
        // Save failed (e.g. the ability isn't enabled at the app level) — revert
        // the switch to its prior position so it never shows a state the backend
        // didn't persist. Mirrors the admin panel's hold-until-confirmed UX.
        toggle.checked = !toggle.checked;
        _flashSaveCheck(wrap, false);
      } finally {
        toggle.disabled = false;
      }
    });
  }

  // ── Wire Save button (inside the body) ──
  const saveBtn = card.querySelector('.conn-save-btn');
  if (saveBtn && canEdit) {
    saveBtn.addEventListener('click', () => {
      const config = _collectConfig(card, abilityId);
      onSave(abilityId, toggle ? toggle.checked : enabled, config);
    });
  }

  // ── Wire OAuth connect/disconnect ──
  card.querySelectorAll('[data-oauth-connect]').forEach(btn => {
    btn.addEventListener('click', () => {
      const provider = btn.dataset.oauthConnect;
      if (agent && window._oauthConnectFromAgent) {
        window._oauthConnectFromAgent(provider, agent, conn, card);
      }
    });
  });
  card.querySelectorAll('[data-oauth-disconnect]').forEach(btn => {
    btn.addEventListener('click', () => {
      const provider = btn.dataset.oauthDisconnect;
      if (agent && window._oauthDisconnectFromAgent) {
        window._oauthDisconnectFromAgent(provider, agent, conn, card);
      }
    });
  });

  // ── Wire per-ability toggles inside OAuth cards ──
  card.querySelectorAll('.conn-ability-row').forEach(row => {
    const abToggle = row.querySelector('.conn-ability-toggle');
    if (!abToggle) return;
    abToggle.addEventListener('change', async () => {
      const abId = row.dataset.abilityId;
      const ab = (conn._abilities || []).find(a => a.id === abId) || {};
      // Spinner → ✓ / ⚠ confirm overlaid on the toggle, parity with the main one.
      const wrap = abToggle.closest('.ac-ability-toggle-wrap');
      _markSaving(wrap);
      abToggle.disabled = true;
      try {
        if (window._onAbilityToggle) {
          await window._onAbilityToggle(agent, conn, card, abId, abToggle.checked, ab.source);
        }
        _flashSaveCheck(wrap, true);
      } catch (e) {
        // Revert the switch — the backend didn't persist it.
        abToggle.checked = !abToggle.checked;
        _flashSaveCheck(wrap, false);
      } finally {
        abToggle.disabled = false;
      }
    });
  });

  return card;
}

// ── Agent-specific: ability config body (companion .json schema + tool perms) ──

async function _buildAbilityBody(conn, { agent, canEdit, abilitiesByProvider, onSave, userId, agentToolsLoader }) {
  const abilityId = conn.connection_type;
  const needsConfig = conn.simple === false;

  // EVERY ability connection gets a body now (even simple, tool-less ones) so
  // each row is expandable. Editing is gated to admins, not source==='custom'.
  const el = document.createElement('div');
  el.className = 'conn-fields ac-ability-body';

  // Forward-declared so the tool/skill eye handlers (built below) can refresh the
  // whole-ability schema preview; assigned where that preview is built.
  let _refreshSchema = () => {};

  // ── Visibility info line — a transient blurb that types itself in (left→right)
  // just below the eye when a visibility is toggled, holds ~1.8s, then erases
  // itself (left→right) and vanishes. Right-aligned so it sits under the eyes.
  const infoEl = document.createElement('div');
  infoEl.className = 'ability-vis-info';
  infoEl.style.cssText = 'font-size:11px;margin:0 0 6px;min-height:14px;text-align:right;color:var(--accent);';
  let _typeTimer = null;
  const _msgFor = (kind, mode) => {
    const disc = mode === 'discoverable';
    if (kind === 'ability') return disc ? 'This ability is discoverable to the agent.' : 'This ability is always visible to the agent.';
    if (kind === 'skill')   return disc ? 'This skill is discoverable to the agent.' : 'This skill is visible to the agent.';
    return disc ? 'This tool is discoverable to the agent.' : 'This tool is visible to the agent.';
  };
  const _setVisInfo = (kind, mode) => {
    const msg = _msgFor(kind, mode);
    infoEl.style.color = mode === 'discoverable' ? 'var(--fg-3)' : 'var(--accent)';
    if (_typeTimer) { clearInterval(_typeTimer); _typeTimer = null; }
    let i = 0;
    _typeTimer = setInterval(() => {            // type in, left → right
      infoEl.textContent = msg.slice(0, ++i);
      if (i >= msg.length) {
        clearInterval(_typeTimer); _typeTimer = null;
        setTimeout(() => {                       // hold, then erase from the left
          let j = 0;
          _typeTimer = setInterval(() => {
            infoEl.textContent = msg.slice(++j);
            if (j >= msg.length) { clearInterval(_typeTimer); _typeTimer = null; infoEl.textContent = ''; }
          }, 14);
        }, 1800);
      }
    }, 22);
  };
  el.appendChild(infoEl);
  el._setVisInfo = _setVisInfo;  // header eye toggle calls this on change

  // ── Skill row (if the ability ships a how-to skill) — eye toggle + view ────
  if (conn.has_skill) {
    const skillRow = document.createElement('div');
    skillRow.className = 'ability-skill-row';
    skillRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin:2px 0 8px;';

    const skillEye = _buildEyeToggle({
      value: conn.skill_mode === 'visible' ? 'visible' : 'discoverable',
      canEdit,
      onChange: async (mode) => {
        const res = await fetch(`/api/v1/agents/${agent.id}/abilities/${encodeURIComponent(abilityId)}/skill-visibility`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ user_id: userId, mode }),
        });
        if (!res.ok) throw new Error('save failed');
        conn.skill_mode = mode;
        _setVisInfo('skill', mode);
        _refreshSchema();
      },
      titleVisible: "Visible — the skill's full how-to is in the agent's prompt every turn",
      titleDiscoverable: 'Discoverable — the agent loads the skill on demand',
    });

    const cap = document.createElement('span');
    cap.style.cssText = 'font-size:11px;font-weight:600;color:var(--fg-main);';
    cap.textContent = 'Skill';

    const sum = document.createElement('span');
    sum.style.cssText = 'font-size:11px;color:var(--fg-3);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
    sum.textContent = conn.skill_summary || 'How-to for this ability';
    sum.title = conn.skill_summary || '';

    const viewBtn = document.createElement('button');
    viewBtn.type = 'button';
    viewBtn.className = 'agents-btn';
    viewBtn.style.cssText = 'font-size:10px;padding:2px 8px;flex-shrink:0;';
    viewBtn.textContent = 'View';

    // Eye on the far right so it lines up with the tool + ability eyes.
    skillRow.appendChild(cap);
    skillRow.appendChild(sum);
    skillRow.appendChild(viewBtn);
    skillRow.appendChild(skillEye);
    el.appendChild(skillRow);

    const skillBody = document.createElement('div');
    skillBody.style.cssText = 'display:none;margin:0 0 8px;';
    el.appendChild(skillBody);
    let _skillLoaded = false;
    viewBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const open = skillBody.style.display === 'none';
      skillBody.style.display = open ? 'block' : 'none';
      viewBtn.textContent = open ? 'Hide' : 'View';
      if (open && !_skillLoaded) {
        _skillLoaded = true;
        skillBody.innerHTML = '<div style="font-size:11px;color:var(--fg-3);">Loading…</div>';
        try {
          const r = await fetch('/api/v1/abilities/' + encodeURIComponent(abilityId) + '/skill');
          const d = r.ok ? await r.json() : null;
          skillBody.innerHTML = '';
          const pre = document.createElement('pre');
          pre.style.cssText = 'margin:0;padding:6px 8px;background:var(--bg-1);border:1px solid var(--border);border-radius:5px;font-size:10px;white-space:pre-wrap;overflow:auto;max-height:280px;color:var(--fg-2);';
          pre.textContent = (d && d.content) || 'No skill text.';
          skillBody.appendChild(pre);
        } catch (_) {
          skillBody.innerHTML = '<div style="font-size:11px;color:var(--danger);">Could not load skill.</div>';
        }
      }
    });
  }

  // ── Credentials section — for abilities that declare a `credentials` block.
  // User/agent-scoped credentials (e.g. Browser Cookies) are entered HERE, in the
  // agent's own panel, by the agent's user — the admin table can't, since the
  // secret is per-user. Shared with the admin table via buildCredentialsSection so
  // the two panels render the identical form (SISTER-PANEL contract). Self-removes
  // when the ability declares no credentials, so it's safe to add unconditionally;
  // we still gate on the row's has_credentials flag to skip the probe fetch.
  if (conn.has_credentials) {
    el.appendChild(buildCredentialsSection(abilityId, {
      agentId: agent ? agent.id : null,
      onSaved: () => { invalidateAbilityCatalog(); },
    }));
  }

  // ── Unified settings + tools list ───────────────────────────────────────────
  // This ability's config knobs (this section, loaded async) AND its tool rows
  // (built below) render as ONE continuous run of rows that sits DIRECTLY in this
  // expanded body — no bordered sub-table box around it (the frame is stripped in
  // CSS; see `.ac-config-settings`). Every row is the SAME stacked `.ac-ability-
  // row` (title + control inline, description on its own line below).
  // (SISTER-PANEL: AGENT-ABILITY-TABLE.)
  const unifiedList = _buildAbilitySettingsList();
  // Tool rows append AFTER this anchor; config rows (async) insert BEFORE it — so
  // config always lands above the tools regardless of which finishes first.
  const toolsAnchor = document.createComment('tools');
  unifiedList.appendChild(toolsAnchor);
  el.appendChild(unifiedList);
  // Drop the whole list if it ends up with no rows (e.g. a simple, tool-less
  // ability) so the expanded body stays clean.
  const _pruneUnified = () => {
    if (!unifiedList.querySelector('.ac-ability-row')) unifiedList.remove();
  };

  // ── Settings rows (non-simple abilities) ──
  if (needsConfig) {
    const ph = document.createElement('div');
    ph.className = 'conn-loading';
    ph.style.cssText = 'padding:8px;font-size:11px;';
    ph.textContent = 'Loading settings…';
    unifiedList.insertBefore(ph, toolsAnchor);

    // Load companion .json schema asynchronously, then drop its rows in ABOVE the
    // tools anchor (config first, tools after).
    _loadConfigSchema(abilityId, conn, agent).then(res => {
      ph.remove();
      if (!res || !res.rowsHtml) { _pruneUnified(); return; }
      const tmp = document.createElement('div');
      tmp.innerHTML = res.rowsHtml;
      [...tmp.children].forEach(r => unifiedList.insertBefore(r, toolsAnchor));
      if (res.note) {
        const noteEl = document.createElement('div');
        noteEl.className = 'ac-config-note';
        noteEl.textContent = res.note;
        el.appendChild(noteEl);
      }
      const fields = [...unifiedList.querySelectorAll('.ability-setting-field')];
      if (!fields.length) return;
      if (!canEdit) { fields.forEach(f => { f.disabled = true; }); return; }
      // Apply the admin's GLOBAL ceiling to each control BEFORE wiring saves: a
      // binding boolean is locked to the admin value, a numeric cap sets the
      // field's max/min + clamps. This is the per-agent face of the global limit;
      // the runtime clamps the same way regardless (app/admin/ability_config.py).
      fields.forEach(f => _applyConfigCeiling(f));
      // Persist each config on change, with the shared spinner → "Saved" feedback.
      // Previously these fields (e.g. Automation's "Allow Recurring Schedules")
      // were only collected when the ability's ENABLE toggle flipped, so editing
      // a setting on its own silently did nothing. Now every change saves the full
      // settings set on the spot. The current enabled state is passed through so a
      // config save never accidentally flips the ability on/off.
      const collect = () => {
        const settings = {};
        fields.forEach(f => { settings[f.dataset.settingKey] = f.value; });
        return { ability_settings: settings };
      };
      // Read the LIVE enable state off the header toggle (conn.enabled can be
      // stale if the ability was toggled on/off earlier this session), so saving
      // a config never accidentally flips the ability's on/off state.
      const liveEnabled = () => {
        const t = el.closest('.conn-card')?.querySelector('.conn-toggle');
        return t ? t.checked : !!conn.enabled;
      };
      fields.forEach(f => {
        // The spinner → ✓ / ⚠ confirm overlays ON TOP of the field's control slot.
        const ctrl = f.closest('.ac-config-control');
        // Remember the last-saved value so a failed save can roll the field back.
        let prevVal = f.value;
        f.addEventListener('change', async () => {
          f.disabled = true;
          _markSaving(ctrl);                     // spinner over the field
          try {
            await Promise.resolve(onSave(abilityId, liveEnabled(), collect()));
            prevVal = f.value;
            _flashSaveCheck(ctrl, true);         // ✓
          } catch (e) {
            f.value = prevVal;                   // revert to the last saved value
            _flashSaveCheck(ctrl, false);        // ⚠
          } finally {
            f.disabled = false;
          }
        });
      });
    });
  }

  // ── Resolve this ability's tools ──
  // Prefer per-agent resolved tools (richer: description, mode, locked,
  // destructive) filtered to this ability; fall back to the static catalog map.
  let toolMetas = [];
  // Resolve the agent's tools on demand (cached by the loader) now that the
  // ability is being opened, then keep only this ability's tools.
  let agentTools = [];
  try {
    agentTools = (typeof agentToolsLoader === 'function') ? (await agentToolsLoader()) : [];
  } catch (_) {
    agentTools = [];
  }
  const resolved = Array.isArray(agentTools)
    ? agentTools.filter(t => t.ability === abilityId)
    : [];
  if (resolved.length) {
    toolMetas = resolved.map(t => ({
      name: t.name,
      description: t.description || '',
      mode: t.mode || 'visible',
      locked: !!t.locked,
      destructive: !!t.destructive,
      schema: t.schema,
      tokensVisible: t.tokens_visible,
      tokensDiscoverable: t.tokens_discoverable,
      // The admin's GLOBAL per-tool limit (Auto < Ask < Deny). The permission tri
      // clamps this agent to it — it may match or be stricter, never looser.
      ceiling: t.ceiling || 'auto',
    }));
  } else {
    const fallback = (ABILITY_TO_TOOLS || {})[abilityId] || [];
    toolMetas = fallback.map(name => ({
      name, description: '', mode: 'always', locked: false, destructive: false,
    }));
  }

  // ── Permission state for this agent ──
  const sp0 = (agent.safety_policy && typeof agent.safety_policy === 'object') ? agent.safety_policy : {};
  const blockedSet = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  const dtSet = new Set(Array.isArray(sp0.destructive_tools) ? sp0.destructive_tools : []);
  let _saveTimer = null;
  let _saveWaiters = [];
  // Debounced save that RESOLVES with the save's success so a permission control
  // can show its spinner until the real save lands, then flash Saved / Failed.
  // Rapid changes coalesce into one fetch; every caller's promise settles together.
  function persist() {
    if (_saveTimer) clearTimeout(_saveTimer);
    return new Promise((resolve) => {
      _saveWaiters.push(resolve);
      _saveTimer = setTimeout(async () => {
        _saveTimer = null;
        const waiters = _saveWaiters; _saveWaiters = [];
        const newSp = { ...sp0, destructive_tools: [...dtSet] };
        let ok = true;
        try {
          const res = await fetch(`/api/v1/agents/${agent.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ user_id: userId, allowed_tools: [...blockedSet], safety_policy: newSp }),
          });
          ok = res.ok;
        } catch (e) { ok = false; }
        waiters.forEach(r => r(ok));
      }, 400);
    });
  }

  // ── Tool rows — appended into the SAME unified list, after the config rows ──
  if (toolMetas.length) {
    for (const meta of toolMetas) {
      meta.permMode = blockedSet.has(meta.name)
        ? 'deny'
        : ((dtSet.has(meta.name) || meta.destructive) ? 'ask' : 'auto');
      const toolRow = _buildToolRow(meta, {
        canEdit,
        showPerm: true,
        onPermissionChange: (name, mode) => {
          blockedSet.delete(name); dtSet.delete(name);
          if (mode === 'deny') blockedSet.add(name);
          else if (mode === 'ask') dtSet.add(name);
          return persist();  // resolves with save success → drives the row's spinner/Saved
        },
        onVisibilityChange: async (name, mode) => {
          const res = await fetch(`/api/v1/agents/${agent.id}/tools/${encodeURIComponent(name)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ user_id: userId, mode }),
          });
          if (!res.ok) throw new Error('save failed');
          _setVisInfo('tool', mode);
          _refreshSchema();
        },
      });
      unifiedList.appendChild(toolRow);
    }
  }
  // Prune now if there are no config rows to wait for (config prunes itself once
  // its async load resolves).
  if (!needsConfig) _pruneUnified();

  // ── Whole-ability schema preview — the ACTUAL text the agent receives for THIS
  // ability given its current config: hidden ability → the discover line; fully
  // visible → every tool's complete schema + skill; partially visible → the real
  // mix (visible tools' schemas + discoverable tools' one-liners). Recomputes
  // server-side whenever any eye toggles.
  {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin:6px 0 2px;';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'agents-btn';
    btn.style.cssText = 'font-size:10px;padding:2px 8px;';
    btn.textContent = 'See schema';
    const body = document.createElement('div');
    body.style.cssText = 'display:none;margin-top:4px;';
    wrap.appendChild(btn);
    wrap.appendChild(body);
    el.appendChild(wrap);

    async function _load() {
      body.innerHTML = '<div style="font-size:11px;color:var(--fg-3);">Loading…</div>';
      try {
        const r = await fetch(`/api/v1/agents/${agent.id}/abilities/${encodeURIComponent(abilityId)}/schema-preview?user_id=${encodeURIComponent(userId)}`, { headers: authHeaders() });
        const d = r.ok ? await r.json() : null;
        if (!d) throw new Error('failed');
        const label = d.state === 'full' ? 'Fully visible'
          : (d.state === 'hidden' ? 'Discoverable (hidden until loaded)' : 'Partially visible');
        body.innerHTML = '';
        const capEl = document.createElement('div');
        capEl.style.cssText = 'font-size:10px;color:var(--fg-3);margin-bottom:3px;';
        capEl.textContent = `${label} — what the agent receives (≈${d.tokens} tok):`;
        const pre = document.createElement('pre');
        pre.style.cssText = 'margin:0;padding:6px 8px;background:var(--bg-1);border:1px solid var(--border);border-radius:5px;font-size:10px;white-space:pre-wrap;overflow:auto;max-height:320px;color:var(--fg-2);';
        pre.textContent = d.text || '(nothing)';
        body.appendChild(capEl);
        body.appendChild(pre);
      } catch (_) {
        body.innerHTML = '<div style="font-size:11px;color:var(--danger);">Could not load schema.</div>';
      }
    }
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = body.style.display === 'none';
      body.style.display = open ? 'block' : 'none';
      btn.textContent = open ? 'Hide schema' : 'See schema';
      if (open) _load();
    });
    // Assign the forward-declared refresher: re-fetch only while the panel is open.
    _refreshSchema = () => { if (body.style.display !== 'none') _load(); };
    el._refreshSchema = _refreshSchema;  // header ability eye calls this
  }

  return el;
}

// ── Config-knob ceiling (PER-AGENT tree only) ─────────────────────────────────
// Each per-agent config control carries `data-ceiling` (off/on/max/min) + the
// admin's app-level `data-ceiling-value` (emitted by _renderSettingInput from the
// config-schema endpoint). Apply the admin's GLOBAL maximum to the control so the
// agent can only MATCH it or be stricter, never looser:
//   • boolean ("off"/"on") — when the admin holds the binding value, lock the
//     select to it (off can't be enabled / on can't be disabled).
//   • number ("max"/"min") — set the input's max/min to the admin value + clamp.
// A small 🔒 "set by admin" note makes the limit visible. The runtime clamps the
// same way (app/admin/ability_config.effective_ability_config), so this is purely
// the honest UI face of the cap. The ADMIN table never calls this (it IS the
// ceiling). (SISTER-PANEL: AGENT-ABILITY-TABLE — the control LOOK stays shared;
// only this read-only clamp is agent-side.)
function _applyConfigCeiling(f) {
  const rule = f.dataset.ceiling;
  if (!rule) return;
  const cval = f.dataset.ceilingValue;
  const label = f.closest('.ac-ability-row')?.querySelector('.ac-ability-label');
  const addNote = (text) => {
    if (!label || label.querySelector('.ac-ceiling-note')) return;
    const n = document.createElement('span');
    n.className = 'ac-ceiling-note ac-ability-desc';
    n.style.cssText = 'color:var(--warning);font-size:10px;';
    n.textContent = '\u{1F512} ' + text;   // 🔒
    label.appendChild(n);
  };
  if (rule === 'off' || rule === 'on') {
    const adminOn = ['1', 'true', 'yes', 'on', 'enabled'].includes(String(cval).trim().toLowerCase());
    if (rule === 'off' && !adminOn) {
      f.value = 'false'; f.disabled = true;
      f.title = 'Disabled by the admin (global limit) — cannot be enabled per agent';
      addNote('Disabled by admin');
    } else if (rule === 'on' && adminOn) {
      f.value = 'true'; f.disabled = true;
      f.title = 'Required on by the admin (global limit) — cannot be disabled per agent';
      addNote('Required by admin');
    }
    return;
  }
  if (rule === 'max' || rule === 'min') {
    const cap = parseFloat(cval);
    if (isNaN(cap)) return;
    const clampNum = () => {
      const v = parseFloat(f.value);
      if (isNaN(v)) return;
      if (rule === 'max' && v > cap) f.value = String(cap);
      if (rule === 'min' && v < cap) f.value = String(cap);
    };
    if (rule === 'max') f.max = String(cap); else f.min = String(cap);
    clampNum();
    f.addEventListener('input', clampNum);
    addNote(`Admin ${rule === 'max' ? 'max' : 'min'} ${cap}`);
  }
}

async function _loadConfigSchema(abilityId, conn, agent) {
  try {
    const res = await fetch('/api/v1/abilities/' + encodeURIComponent(abilityId) + '/config-schema');
    if (!res.ok) return null;
    const schema = await res.json();
    if (!schema || !schema.settings || !schema.settings.length) return null;

    const current = (conn.config && conn.config.ability_settings) || {};
    // Render as BARE config-setting rows via the shared builder; the caller drops
    // them into the SAME unified `.ac-list` as this ability's tool rows, so config
    // knobs and tools read as one table. (SISTER-PANEL: AGENT-ABILITY-TABLE.)
    return {
      rowsHtml: _buildSettingsRowsHtml(schema, current, 'ability-setting-field', 'data-setting-key'),
      note: schema.note || '',
    };
  } catch (e) {
    return null;
  }
}

// (The per-tool "See schema" preview was replaced by a single whole-ability
// schema preview built inline in _buildAbilityBody — it composes the ACTUAL text
// the agent receives for the ability given its current visibility config, server
// side via GET /agents/{id}/abilities/{ability}/schema-preview.)

// (Removed: `_buildToolPermissionTable` — the legacy Auto/Ask/Deny <select>
// table. Replaced by the shared `_buildToolRow` builder in shared/dom-utils.js,
// which carries BOTH the permission and the visibility controls and is shared
// with the admin panel for guaranteed parity.)

// ── Agent-specific: Telegram body ─────────────────────────────────────────────

function _buildTelegramBody(conn, { agent, canEdit, onSave }) {
  const el = document.createElement('div');
  el.className = 'conn-fields ac-ability-body';

  if (!canEdit) {
    const tokenVal = conn.config?.bot_token || '';
    el.innerHTML = `
      <label class="conn-field-label">Bot Token</label>
      <div class="conn-token-row">
        <input type="text" class="agents-input" value="${tokenVal ? '\u2022\u2022\u2022\u2022\u2022\u2022' + _esc(tokenVal.slice(-4)) : 'Not configured'}" readonly style="font-family:monospace;opacity:0.6;">
      </div>
    `;
    return el;
  }

  const tokenVal = conn.config?.bot_token || '';
  const agentId = agent ? agent.id : null;
  el.innerHTML = `
    <label class="conn-field-label">Bot Token</label>
    <div class="conn-token-row">
      <input type="text" class="conn-token-input agents-input" placeholder="Enter bot token..." value="${_esc(tokenVal)}" autocomplete="off" data-lpignore="true" data-1p-ignore="true" style="font-family:monospace;">
      <button class="agents-btn primary conn-save-btn">Save</button>
    </div>
    <span class="conn-field-hint">From <a href="https://t.me/BotFather" target="_blank" style="color:var(--brand)">@BotFather</a> &mdash; format: <code style="font-size:10px;color:var(--fg-2);">1234567890:ABCdef...</code></span>
    <div class="conn-tg-mode-info" style="margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:18px;"></div>
    <div style="margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <button class="agents-btn conn-tg-test-btn" data-agent-id="${agentId || ''}" data-conn-type="telegram" style="font-size:11px;padding:4px 10px;">Test Connection</button>
      <span class="conn-tg-test-status" style="font-size:11px;"></span>
    </div>
    <span class="conn-save-msg"></span>
  `;
  return el;
}

// ── Agent-specific: OAuth / integration bodies ────────────────────────────────

function _buildOAuthBody(conn, { agent, canEdit, abilitiesByProvider, onSave }) {
  const el = document.createElement('div');
  el.className = 'conn-fields ac-ability-body';

  const _OAUTH_PROVIDERS = {
    google:    { label: 'Google Account',     hint: 'Link your Google account for Gmail, Drive, Docs, and Calendar access.' },
    microsoft: { label: 'Microsoft Account',   hint: 'Link your Microsoft account for Outlook, OneDrive, and SharePoint access.' },
    yahoo:     { label: 'Yahoo Account',       hint: 'Link your Yahoo account for Yahoo Mail access.' },
    dropbox:   { label: 'Dropbox Account',     hint: 'Link your Dropbox account for file storage and sharing access.' },
    meta:      { label: 'Meta Account',        hint: 'Link your Meta account for Facebook and Instagram access.' },
    twitter:   { label: 'X (Twitter) Account', hint: 'Link your X account to read and post tweets.' },
    linkedin:  { label: 'LinkedIn Account',    hint: 'Link your LinkedIn account for profile and company page access.' },
    tiktok:    { label: 'TikTok Account',      hint: 'Link your TikTok account for video and account access.' },
    pinterest: { label: 'Pinterest Account',   hint: 'Link your Pinterest account for board and pin access.' },
    reddit:    { label: 'Reddit Account',      hint: 'Link your Reddit account to read and post to subreddits.' },
    snapchat:  { label: 'Snapchat Account',    hint: 'Link your Snapchat account for story and audience access.' },
    twitch:    { label: 'Twitch Account',      hint: 'Link your Twitch account for channel and stream data access.' },
    ebay:      { label: 'eBay Account',        hint: 'Link your eBay seller account to search, list, and manage items.' },
    etsy:      { label: 'Etsy Account',        hint: 'Link your Etsy account to search listings and manage your shop.' },
    shopify:   { label: 'Shopify Store',       hint: 'Connect your Shopify store to read and manage products and orders.' },
    amazon:    { label: 'Amazon Seller',       hint: 'Link your Amazon Selling Partner account (region required).' },
  };

  const ct = conn.connection_type;
  // Resolve Meta aliases (facebook/instagram both use the meta provider)
  const provider = ct === 'facebook' || ct === 'instagram' ? 'meta' : ct;
  const oauthInfo = _OAUTH_PROVIDERS[provider];
  if (!oauthInfo) {
    el.innerHTML = '';
    return el;
  }

  const connected = conn[`${ct}_connected`];
  const picture   = conn[`${ct}_picture`] || '';
  const name      = conn[`${ct}_name`] || '';
  const email     = conn[`${ct}_email`] || '';
  const connAt    = conn[`${ct}_connected_at`] || '';
  const abilitiesHtml = _renderOAuthAbilitiesBlock(conn._abilities || [], canEdit, agent);

  if (connected) {
    el.innerHTML = `
      <div class="conn-google-account">
        ${picture ? `<img src="${_esc(picture)}" alt="" style="width:28px;height:28px;border-radius:50%;">` : ''}
        <div style="flex:1;min-width:0;">
          <div style="font-weight:500;font-size:12px;">${_esc(name)}</div>
          <div style="font-size:11px;color:var(--fg-3);overflow:hidden;text-overflow:ellipsis;">${_esc(email)}</div>
        </div>
      </div>
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center;">
        <button class="agents-btn" data-oauth-disconnect="${ct}" style="color:var(--danger);border-color:var(--danger);">Disconnect</button>
        ${connAt ? `<span style="font-size:10px;color:var(--fg-3);">Connected ${new Date(connAt).toLocaleDateString()}</span>` : ''}
      </div>
      ${abilitiesHtml}
      <span class="conn-save-msg"></span>
    `;
  } else if (!canEdit && !conn.enabled) {
    el.innerHTML = `<span style="font-size:12px;color:var(--fg-3);">Not enabled by admin</span>`;
  } else {
    el.innerHTML = `
      <button class="agents-btn primary" data-oauth-connect="${ct}" style="width:100%;">Connect ${oauthInfo.label}</button>
      <span class="conn-field-hint" style="margin-top:6px;">${oauthInfo.hint}</span>
      ${abilitiesHtml}
      <span class="conn-save-msg"></span>
    `;
  }

  return el;
}

// ── Per-ability toggles inside OAuth provider cards ────────────────────────

function _renderOAuthAbilitiesBlock(abilities, canEdit, agent) {
  const list = (abilities || []).filter(a => !a.implicit && a.mode !== 'disabled');
  if (!list.length) return '';

  const byoAllowed = canEdit && list.some(a => a.mode === 'byo_only' || a.mode === 'both');
  const byoLink = byoAllowed
    ? `<button class="conn-byo-setup-btn" data-provider="${_esc(list[0].provider)}"
                style="background:none;border:none;color:var(--accent);font-size:10px;cursor:pointer;padding:0;margin-left:auto;">
         Use my own credentials →
       </button>`
    : '';

  const rows = list.map(ab => {
    const cantToggle = !canEdit;
    const checked = ab.enabled ? 'checked' : '';
    const dis = cantToggle ? 'disabled' : '';
    const sourceTag = ab.source === 'byo'
      ? '<span style="font-size:9px;background:var(--bg-2);color:var(--accent);padding:1px 4px;border-radius:3px;margin-left:6px;font-weight:600;">BYO</span>'
      : '';
    const byoConfigured = ab.byo_configured
      ? '<span title="BYO credentials configured" style="font-size:10px;color:var(--success);margin-left:6px;">●</span>'
      : '';
    return `
      <div class="conn-ability-row ac-ability-row" data-ability-id="${_esc(ab.id)}" data-provider="${_esc(ab.provider)}">
        <div class="ac-ability-label">
          <div class="ac-ability-name" style="font-size:12px;">
            ${_esc(ab.display_name)}${sourceTag}${byoConfigured}
          </div>
          <div class="ac-ability-desc">${_esc(ab.description || '')}</div>
        </div>
        <span class="ac-ability-status"></span>
        <label class="conn-toggle-wrap ac-ability-toggle-wrap" title="${cantToggle ? 'Admin only' : (ab.enabled ? 'Disable' : 'Enable')}">
          <input type="checkbox" class="conn-toggle conn-ability-toggle" ${checked} ${dis}>
          <span class="conn-toggle-track"></span>
        </label>
      </div>
    `;
  }).join('');

  return `
    <div class="conn-abilities-block ac-list" style="margin-top:10px;">
      <div class="ac-ability-row conn-abilities-head">
        <span class="ac-ability-name" style="font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--accent);">Abilities</span>
        ${byoLink}
      </div>
      ${rows}
    </div>
  `;
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function _getIcon(abilityId) {
  // Imported directly from agents/state.js (NOT a window global). Falls back
  // to the plug icon for an unknown ability.
  return (_CONN_ICONS && _CONN_ICONS[abilityId]) || 'plug';
}

// ── "Available to" — per-agent caller-access gate ──────────────────────────────
// A menu section in each ability row's ⋯ menu choosing WHICH CALLER may trigger
// the ability when chatting THIS agent: Everyone (anyone incl. anonymous guests —
// the default) · Registered (signed-in accounts only) · Admins (admins only). It
// is a REAL boundary enforced server-side at tool-assembly time (the gated
// ability's tools are never built for a caller below the level) — not a prompt
// hint the model could ignore. PER-AGENT ONLY, like the visibility mode and the
// config ceiling: the admin global ability table has no equivalent control, so
// there is nothing to mirror there. (SISTER-PANEL: AGENT-ABILITY-TABLE.)
const _ACCESS_LEVELS = [
  { value: 'everyone',   label: 'Everyone',   icon: 'globe',      title: 'Available to anyone, including not-signed-in guests (default)' },
  { value: 'registered', label: 'Registered', icon: 'user-check', title: 'Available only to signed-in (registered) accounts' },
  { value: 'admin',      label: 'Admins',     icon: 'shield',     title: 'Available only to admin users' },
];

function _collectConfig(card, abilityId) {
  const config = {};
  if (abilityId === 'telegram') {
    const tokenInput = card.querySelector('.conn-token-input');
    if (tokenInput) {
      config.bot_token = (tokenInput.dataset.realValue ?? tokenInput.value).trim();
    }
  }
  // Collect ability settings from the schema fields
  const settingFields = card.querySelectorAll('.ability-setting-field');
  if (settingFields.length) {
    const settings = {};
    settingFields.forEach(f => {
      settings[f.dataset.settingKey] = f.value;
    });
    config.ability_settings = settings;
  }
  return config;
}

// ── Search filtering ──────────────────────────────────────────────────────────
// Live filter driven by the hybrid search/chat pill's `onFilter`. Same behaviour
// as the old search box: expand groups while searching, show matching cards, hide
// emptied groups, restore everything when the query is cleared.

function _runAgentFilter(rawQuery, grid, indexed) {
  const q = (rawQuery || '').trim().toLowerCase();

  // Expand all groups while searching so members are reachable
  const groupEls = grid.querySelectorAll('.ac-group');
  groupEls.forEach(g => g.classList.toggle('expanded', !!q));

  // Filter rows
  for (const { card, haystack } of indexed) {
    card.style.display = (!q || haystack.includes(q)) ? '' : 'none';
  }

  // Hide groups whose cards are all filtered out
  if (q) {
    groupEls.forEach(g => {
      const anyVisible = [...g.querySelectorAll('.conn-card')].some(c => c.style.display !== 'none');
      g.style.display = anyVisible ? '' : 'none';
    });
  } else {
    groupEls.forEach(g => { g.style.display = ''; });
  }
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║  MAIN BUILD FUNCTION  —  PROGRESSIVE (LAZY) LOADING                         ║
// ╚══════════════════════════════════════════════════════════════════════════════╝
//
// The Abilities tab loads in four staged steps so it paints fast and never shows
// a blank panel (SISTER-PANEL note: this staging is AGENT-TAB ONLY — the admin
// table gets every ability's enable state baked into the single catalog fetch, so
// it has nothing extra to wait for and renders its tris immediately):
//
//   1. Spinner          — shown instantly, before any fetch (`_buildAbilityTreeLoader`).
//   2. Group heads      — built from the catalog alone (no per-agent data), each
//                         with a SPINNER where its bulk toggle will go.
//   3. Toggle statuses  — the per-agent /connections fetch (kicked off in parallel
//                         with step 2) lands → each group's spinner is swapped for
//                         the real Off·Mixed·On tri, empty groups are hidden.
//   4. Group contents   — a group's member cards are built only when it is first
//                         expanded (and each ability's tools/skill/config body is
//                         still built lazily inside the card on its own expand).
//
// So the only thing the FIRST paint waits on is the catalog (cached after the first
// page load); the heavier per-agent fetch fills in statuses afterwards, and the
// bulk of the DOM (member cards + their bodies) is deferred until the user opens it.

/**
 * Build the agent ability table into `container`.
 *
 * @param {HTMLElement} container - DOM element to render into
 * @param {object} cfg
 * @param {object} cfg.agent - the agent object
 * @param {function} [cfg.connectionsLoader] - () => Promise<{ connections, canEdit }>;
 *        the per-agent ability connections + edit permission, fetched LAZILY in
 *        parallel with the group heads so the first paint never waits on it.
 *        PREFERRED over passing a resolved array.
 * @param {object[]} [cfg.connections] - deprecated: a pre-fetched connections array
 *        (used with cfg.canEdit); still honoured for back-compat (wrapped into a loader).
 * @param {boolean} [cfg.canEdit] - back-compat: edit permission when cfg.connections is used.
 * @param {object} [cfg.abilitiesByProvider] - per-provider ability toggles (default {})
 * @param {function} cfg.onSave - (connectionType, enabled, config) => Promise
 * @param {boolean} [cfg.showSearch] - show search bar (default true)
 * @param {string} [cfg.userId] - current user id (for per-tool PUTs; avoids app-state coupling)
 * @param {function} [cfg.agentToolsLoader] - () => Promise<tools[]>; resolves the agent's tools LAZILY on first ability-expand (cached by the caller). Preferred over agentTools so the heavy /tools call never blocks the initial list.
 * @param {object[]} [cfg.agentTools] - deprecated: a pre-fetched resolved-tools array; still honoured for back-compat (wrapped into a resolver)
 */
export async function build(container, cfg) {
  if (!container) return null;

  const { agent, onSave, userId } = cfg;
  const abilitiesByProvider = cfg.abilitiesByProvider || {};
  const showSearch = cfg.showSearch !== false;
  // Lazy resolver for the agent's tools — called only when an ability is first
  // expanded. Falls back to a no-op resolver so callers that still pass a static
  // `agentTools` array keep working.
  const agentToolsLoader = cfg.agentToolsLoader
    || (Array.isArray(cfg.agentTools) ? (() => Promise.resolve(cfg.agentTools)) : (() => Promise.resolve([])));
  // Per-agent connection state. PREFERRED: cfg.connectionsLoader → a function
  // returning Promise<{ connections, canEdit }>, fetched in PARALLEL with the
  // group heads. Back-compat: a resolved cfg.connections array (+ cfg.canEdit).
  const connectionsLoader = (typeof cfg.connectionsLoader === 'function')
    ? cfg.connectionsLoader
    : (() => Promise.resolve({ connections: cfg.connections || [], canEdit: !!cfg.canEdit }));

  const state = { container, grid: null, indexed: [], triSyncs: [] };

  // ── Step 1 — spinner immediately (before the catalog await) ──────────────────
  container.innerHTML = '';
  container.appendChild(_buildAbilityTreeLoader());

  // ── Step 2 — catalog → group heads (no per-agent data needed yet) ────────────
  const cat = await loadAbilityCatalog();
  const groups = cat.groups || [];
  const allAbilities = cat.abilities || {};

  // Kick the per-agent connections fetch off NOW so it overlaps the head build.
  // Resolves to { byType, canEdit }; `_connData` caches it for the lazy builders.
  let _connData = null;
  const connPromise = Promise.resolve()
    .then(() => connectionsLoader())
    .then(res => {
      const conns = (res && res.connections) || [];
      const byType = {};
      for (const c of conns) {
        if (c.section === 'payment') continue;  // exclude payment-only items
        byType[c.connection_type] = c;
        const provider = c.connection_type === 'facebook' || c.connection_type === 'instagram' ? 'meta' : c.connection_type;
        c._abilities = (abilitiesByProvider && abilitiesByProvider[provider]) || [];
      }
      _connData = { byType, canEdit: !!(res && res.canEdit) };
      return _connData;
    });

  container.innerHTML = '';

  const grid = document.createElement('div');
  grid.className = 'conn-grid ac-list';
  container.appendChild(grid);
  state.grid = grid;

  // Which catalog members belong on the agent tab and will appear in /connections:
  // app-enabled, non-placeholder, ability-kind members (OAuth/channel/credential
  // members are a different section the caller filters out). Deciding this from the
  // catalog's resolved `enabled` lets the group heads render before the per-agent
  // fetch lands, with no flash.
  const appShown = (m) => {
    const a = allAbilities[m];
    if (!a) return false;
    if (a.placeholder || a.kind === 'placeholder') return false;
    if ((a.kind || 'ability') !== 'ability') return false;
    return a.enabled === true || a.locked_on === true;
  };

  // ── Step 4 (deferred) — build a group's member cards on first expand ──────────
  // Returns a cached promise so repeated expands / a bulk-toggle / a search all
  // reuse the one build. Each card still builds its own heavy body (tools, skill,
  // config, schema) lazily when the ability itself is expanded.
  function _ensureGroupBuilt(rec) {
    if (rec.removed) return Promise.resolve();
    if (rec._buildPromise) return rec._buildPromise;
    rec._buildPromise = (async () => {
      let conn = _connData;
      if (!conn) {
        // Per-agent data not in yet — show a loading row until it lands.
        rec.bodyEl.appendChild(_buildGroupBodyLoading());
        try { conn = await connPromise; }
        catch (_) {
          rec.bodyEl.innerHTML = '<div class="conn-loading" style="padding:10px;font-size:11px;color:var(--danger);">Failed to load abilities.</div>';
          return;
        }
        rec.bodyEl.innerHTML = '';
      }
      const canEdit = conn.canEdit;
      for (const m of rec.memberIds) {
        const c = conn.byType[m];
        if (!c) continue;  // app-enabled but not present (e.g. admin-cred gated)
        const catalogAbility = allAbilities[m] || {};
        c.entitlement_allowed = catalogAbility.entitlement_allowed !== false;
        c.entitlement_reason = catalogAbility.entitlement_reason || 'allowed';
        const card = _buildConnectionCard(c, { agent, canEdit, abilitiesByProvider, onSave, userId, agentToolsLoader });
        card.dataset.connType = m;
        rec.bodyEl.appendChild(card);
        state.indexed.push({
          card,
          haystack: ((c.display_name || '') + ' ' + (c.connection_type || '') + ' Agent Tools').toLowerCase(),
        });
      }
      rec.built = true;
      _refreshLucideIcons(rec.bodyEl);
    })();
    return rec._buildPromise;
  }

  // ── Build group heads from the catalog (member cards are deferred) ────────────
  const groupRecs = [];
  for (const group of groups) {
    const memberIds = (group.members || []).filter(appShown);
    if (!memberIds.length) continue;

    const groupEl = _buildGroup(group.id, group.name, group.icon, group.color, group.desc);
    const head = groupEl.querySelector('.ac-group-head');
    const bodyEl = groupEl.querySelector('.ac-group-body');

    // Spinner where the bulk toggle will go (swapped for the real tri in step 3).
    const triSlot = _buildGroupTriSpinner();
    head.appendChild(triSlot);

    const rec = { group, groupEl, head, bodyEl, memberIds, triSlot, built: false, _buildPromise: null };
    groupRecs.push(rec);

    // `_buildGroup`'s own head listener toggles `.expanded`; this one runs after
    // it and builds the body's cards on first expand (ignores tri/button clicks,
    // same as the toggle listener).
    head.addEventListener('click', e => {
      if (e.target.closest('.ac-tri, button')) return;
      if (groupEl.classList.contains('expanded')) _ensureGroupBuilt(rec);
    });

    grid.appendChild(groupEl);
  }

  // ── Hybrid search / chat pill (above the table) ──
  // TYPE → live filter; SEND/ATTACH → chat to WebAgent the manager.
  // SISTER-PANEL: AGENT-ABILITY-TABLE — mirrored in admin-ability-table.js.
  // MOUNT-LOCATION divergence (look/behaviour still mirrored): the agent card keeps
  // this pill INLINE above the table; the admin Agent Settings page mounts the same
  // shared pill at the BOTTOM of the page (showSearch:false + state.runFilter). See
  // the matching note in admin-ability-table.js — not a sister-panel violation.
  if (showSearch) {
    const searchPill = buildAbilitySearchPill(container, {
      // The filter spans ALL cards, so a non-empty query first builds every group's
      // (otherwise deferred) cards — they'd be invisible to the filter otherwise —
      // then runs the normal filter over the now-complete index.
      onFilter: (q) => {
        const query = (q || '').trim();
        if (!query) { _runAgentFilter('', grid, state.indexed); return; }
        Promise.all(groupRecs.map(_ensureGroupBuilt)).then(() => _runAgentFilter(query, grid, state.indexed));
      },
      onChatSend: spawnWebagentAbilityChat,
    });
    container.insertBefore(searchPill.wrap, container.firstChild);
    state.searchPill = searchPill;
  }

  // Render Lucide icons in the heads, then reveal the groups one-by-one.
  if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
  _staggerRevealGroups(grid);

  // ── Step 3 — per-agent statuses arrive: swap each spinner for its status card ──
  connPromise.then(({ byType }) => {
    for (const rec of groupRecs) {
      const present = rec.memberIds.filter(m => byType[m]);
      // No present members (e.g. all admin-cred-gated) — drop the whole group from
      // the DOM (not just display:none, which `_runAgentFilter` would undo on a
      // search-clear) and skip it in the lazy builder.
      if (!present.length) { rec.removed = true; rec.groupEl.remove(); continue; }

      // Togglable = present ability-kind members (locked-on members are present and
      // counted as on, but are never bulk-controlled — bulk control is gone; the
      // status card is display-only).
      const togglable = present
        .filter(m => ((allAbilities[m] || {}).kind || 'ability') === 'ability')
        .map(m => ({ id: m, isEnabled: () => !!(byType[m] && byType[m].enabled) }));

      if (togglable.length) {
        // Status card (Disabled · Partial · Enabled) replaces the old group tri.
        const status = _appendGroupStatus(rec.head, 'ac-group-status-' + rec.group.id, 'off', 'Disabled · Partial · Enabled — read-only summary of this group');
        rec.triSlot.remove();  // the status card was appended after the spinner; drop the spinner
        const sync = () => {
          const onCount = togglable.filter(m => m.isEnabled()).length;
          status.dataset.state = onCount === 0 ? 'off' : (onCount === togglable.length ? 'on' : 'mixed');
          _paintGroupStatus(status);
        };
        sync();
        // Re-sync the card when a member's own toggle flips its enable state.
        rec.bodyEl.addEventListener('ability-enabled-change', () => sync());
      } else {
        _appendGroupStatus(rec.head, 'ac-group-status-' + rec.group.id, 'na', 'No on/off abilities in this group');
        rec.triSlot.remove();
      }
    }
  }).catch(() => {
    // Connections failed — the status cards read "—" so the rows still read
    // complete; each group body shows its own error when expanded.
    for (const rec of groupRecs) {
      _appendGroupStatus(rec.head, 'ac-group-status-' + rec.group.id, 'na', 'Could not load status');
      rec.triSlot.remove();
    }
  });

  return state;
}
