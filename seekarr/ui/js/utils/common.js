    function asBadge(ok) {
      return ok ? '<span class="badge ok">ON</span>' : '<span class="badge off">OFF</span>';
    }
    function asPill(ok, label, title) {
      const t = title ? ` title="${title}"` : '';
      return ok
        ? `<span class="badge ok"${t}>${label}</span>`
        : `<span class="badge off"${t}>${label}</span>`;
    }
    function safe(v) {
      const text = (v === null || v === undefined) ? '' : String(v);
      return text
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }
    function showToast(title, text, tone='success') {
      const stack = document.getElementById('toast-stack');
      if (!stack) return;
      const toast = document.createElement('div');
      const id = `toast-${++toastSeq}`;
      toast.className = `toast ${tone}`;
      toast.id = id;
      toast.innerHTML = `<div class="toast-title">${safe(title)}</div><div class="toast-text">${safe(text)}</div>`;
      stack.appendChild(toast);
      requestAnimationFrame(() => toast.classList.add('show'));
      window.setTimeout(() => {
        toast.classList.remove('show');
        window.setTimeout(() => {
          const el = document.getElementById(id);
          if (el) el.remove();
        }, 180);
      }, 2600);
    }
    function syncSettingsSaveFab() {
      const fab = document.getElementById('settings-save-fab');
      const msg = document.getElementById('settings-msg');
      const btn = document.getElementById('save-settings');
      if (!fab || !msg || !btn) return;
      const show = settingsDirty || btn.disabled;
      fab.classList.toggle('show', show);
      msg.textContent = settingsStatusMessage || (settingsDirty ? 'Unsaved configuration changes' : '');
    }
    function setSettingsDirtyState(dirty, message='') {
      settingsDirty = !!dirty;
      settingsStatusMessage = message;
      syncSettingsSaveFab();
    }
    async function fetchRecentActionMeta(appType, instanceId, itemKey) {
      const cacheKey = `${String(appType)}:${String(instanceId)}:${String(itemKey || '')}`;
      if (recentItemMetaCache.has(cacheKey)) return recentItemMetaCache.get(cacheKey);
      const resp = await apiFetch(
        `/api/item_meta?app=${encodeURIComponent(String(appType))}&instance_id=${encodeURIComponent(String(instanceId))}&item_key=${encodeURIComponent(String(itemKey || ''))}`,
        { cache: 'default' }
      );
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || `meta ${resp.status}`);
      recentItemMetaCache.set(cacheKey, data || {});
      return data || {};
    }
    function renderActionMetaBadges(kindMeta, sourceLabel = '') {
      const chips = [];
      if (kindMeta.label) chips.push(`<span class="recent-action-kind recent-action-kind-${safe(kindMeta.className)}">${safe(kindMeta.label)}</span>`);
      if (kindMeta.typeLabel) chips.push(`<span class="recent-action-kind recent-action-kind-type">${safe(kindMeta.typeLabel)}</span>`);
      if (sourceLabel) chips.push(`<span class="recent-action-source">${safe(sourceLabel)}</span>`);
      return chips.join('');
    }
    async function hydrateActionMediaRows(root = document) {
      const scope = root || document;
      const rows = Array.from(scope.querySelectorAll(
        '.recent-action-row[data-app][data-instance-id][data-item-key], .history-entry[data-app][data-instance-id][data-item-key]'
      ));
      await Promise.all(rows.map(async (row) => {
        const appType = String(row.getAttribute('data-app') || '').trim();
        const instanceId = String(row.getAttribute('data-instance-id') || '').trim();
        const itemKey = String(row.getAttribute('data-item-key') || '').trim();
        if (!appType || !instanceId || !itemKey) return;
        try {
          const meta = await fetchRecentActionMeta(appType, instanceId, itemKey);
          const button = row.querySelector('.recent-action-link, .history-entry-link');
          if (button && meta.item_url) button.setAttribute('data-item-url', String(meta.item_url));
          const wrap = row.querySelector('.recent-action-cover-wrap, .history-entry-cover-wrap');
          if (wrap && meta.cover_url) {
            const imageClass = wrap.classList.contains('history-entry-cover-wrap')
              ? 'history-entry-cover'
              : 'recent-action-cover';
            wrap.innerHTML = `<img class="${imageClass}" src="${String(meta.cover_url)}" alt="">`;
            wrap.classList.remove('is-empty');
          } else if (wrap) {
            row.classList.add('no-cover');
            wrap.classList.add('is-empty');
            wrap.innerHTML = '';
          }
        } catch (e) {
          const wrap = row.querySelector('.recent-action-cover-wrap, .history-entry-cover-wrap');
          row.classList.add('no-cover');
          if (wrap) {
            wrap.classList.add('is-empty');
            wrap.innerHTML = '';
          }
        }
      }));
    }
    async function openRecentActionItem(appType, instanceId, itemKey) {
      if (!appType || !instanceId || !itemKey) return;
      try {
        const data = await fetchRecentActionMeta(appType, instanceId, itemKey);
        if (!data.item_url) {
          showToast('Open Failed', data.error || 'Could not open this item in Arr.', 'error');
          return;
        }
        window.open(String(data.item_url), '_blank', 'noopener,noreferrer');
      } catch (e) {
        showToast('Open Failed', 'Could not open this item in Arr.', 'error');
      }
    }
    function buildSettingsPayload() {
      const instances = [];
      document.querySelectorAll('#settings-instance-cards [data-key]').forEach(tr => {
        const key = tr.getAttribute('data-key') || '';
        const parts = key.split(':');
        if (parts.length < 2) return;
        const app = parts[0];
        const instance_id = Number(parts[1] || 0);
        instances.push({
          app,
          instance_id,
          instance_name: String(tr.querySelector('.si_name')?.value || '').trim(),
          enabled: !!tr.querySelector('.si_enabled')?.checked,
          interval_minutes: Number(tr.querySelector('.si_interval')?.value || 0),
          search_missing: !!tr.querySelector('.si_missing')?.checked,
          search_cutoff_unmet: !!tr.querySelector('.si_cutoff')?.checked,
          upgrade_scope: String(tr.querySelector('.si_upgrade_scope')?.value || 'wanted'),
          search_order: String(tr.querySelector('.si_search_order')?.value || 'smart'),
          quiet_hours_enabled: !!tr.querySelector('.si_quiet_enabled')?.checked,
          quiet_hours_start: String(tr.querySelector('.si_quiet_start')?.value || '').trim(),
          quiet_hours_end: String(tr.querySelector('.si_quiet_end')?.value || '').trim(),
          min_hours_after_release: Number(tr.querySelector('.si_after_release')?.value || 0),
          min_seconds_between_actions: Number(tr.querySelector('.si_between')?.value || 0),
          max_missing_actions_per_instance_per_sync: Number(tr.querySelector('.si_missing_per_run')?.value || 0),
          max_cutoff_actions_per_instance_per_sync: Number(tr.querySelector('.si_upgrades_per_run')?.value || 0),
          sonarr_missing_mode: (app === 'sonarr') ? String(tr.querySelector('.si_missing_mode')?.value || 'smart') : undefined,
          item_retry_hours: Number(tr.querySelector('.si_retry')?.value || 0),
          rate_window_minutes: Number(tr.querySelector('.si_rate_window')?.value || 0),
          rate_cap: Number(tr.querySelector('.si_rate_cap')?.value || 0),
          arr_url: String(tr.querySelector('.si_url')?.value || '').trim(),
          arr_api_key: String(tr.querySelector('.si_apikey')?.value || '').trim(),
        });
      });
      instances.sort((a, b) => {
        if (a.app !== b.app) return a.app.localeCompare(b.app);
        return a.instance_id - b.instance_id;
      });
      return {
        app: {
          date_format: normalizeDateFormat(document.getElementById('settings-date-format')?.value || 'iso'),
          time_format: normalizeTimeFormat(document.getElementById('settings-time-format')?.value || '24h'),
          quiet_hours_timezone: String(document.getElementById('settings-quiet-timezone')?.value || '').trim(),
        },
        instances,
      };
    }
    function settingsPayloadFingerprint(payload) {
      return JSON.stringify(payload || {
        app: { quiet_hours_timezone: '', date_format: 'iso', time_format: '24h' },
        instances: [],
      });
    }
    function refreshSettingsDirtyState(message='') {
      const current = settingsPayloadFingerprint(buildSettingsPayload());
      setSettingsDirtyState(current !== settingsBaseline, message);
    }
    function syncSleepWindowControls(scope=document) {
      const root = (scope && typeof scope.querySelectorAll === 'function') ? scope : document;
      root.querySelectorAll('.settings-instance-card').forEach(card => {
        const toggle = card.querySelector('.si_quiet_enabled');
        const fields = card.querySelector('.sleep-window-fields');
        const inputs = card.querySelectorAll('.si_quiet_start, .si_quiet_end');
        const enabled = !!toggle?.checked;
        if (fields) fields.classList.toggle('is-disabled', !enabled);
        inputs.forEach(input => {
          input.disabled = !enabled;
          input.setAttribute('aria-disabled', enabled ? 'false' : 'true');
        });
      });
    }
    function confirmDiscardUnsavedSettings(actionLabel) {
      if (!settingsDirty) return true;
      return confirm(`You have unsaved configuration changes. ${actionLabel} will discard them. Continue?`);
    }
    function getTimeZoneLabel() {
      return activeTimeZone ? activeTimeZone : 'local';
    }
    function normalizeDateFormat(value) {
      const fmt = String(value || '').trim().toLowerCase();
      if (fmt === 'us' || fmt === 'mdy' || fmt === 'mm/dd/yyyy') return 'us';
      if (fmt === 'eu' || fmt === 'dmy' || fmt === 'dd/mm/yyyy') return 'eu';
      return 'iso';
    }
    function normalizeTimeFormat(value) {
      const fmt = String(value || '').trim().toLowerCase();
      return (fmt === '12h' || fmt === '12' || fmt === '12hr' || fmt === '12-hour') ? '12h' : '24h';
    }
    function normalizeExternalUrl(value) {
      const raw = String(value || '').trim();
      if (!raw) return '';
      if (/^https?:\/\//i.test(raw)) return raw;
      if (raw.startsWith('//')) return `${window.location.protocol}${raw}`;
      return `${window.location.protocol}//${raw}`;
    }
    function getDateTimeParts(dt, options = {}) {
      const includeSeconds = options.includeSeconds !== false;
      const opts = {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: activeClockFormat === '12h',
      };
      if (activeClockFormat === '24h') opts.hourCycle = 'h23';
      if (includeSeconds) opts.second = '2-digit';
      if (activeTimeZone) opts.timeZone = activeTimeZone;
      const byType = {};
      const parts = new Intl.DateTimeFormat('en-US', opts).formatToParts(dt);
      for (const part of parts) {
        if (part.type !== 'literal') byType[part.type] = part.value;
      }
      const hourRaw = String(byType.hour || '00');
      return {
        year: String(byType.year || dt.getUTCFullYear()),
        month: String(byType.month || '').padStart(2, '0'),
        day: String(byType.day || '').padStart(2, '0'),
        hour: activeClockFormat === '12h' ? String(Number(hourRaw) || 12) : hourRaw.padStart(2, '0'),
        minute: String(byType.minute || '00').padStart(2, '0'),
        second: String(byType.second || '00').padStart(2, '0'),
        dayPeriod: String(byType.dayPeriod || '').toUpperCase(),
      };
    }
    function formatDateFromParts(parts) {
      if (activeDateFormat === 'us') return `${parts.month}/${parts.day}/${parts.year}`;
      if (activeDateFormat === 'eu') return `${parts.day}/${parts.month}/${parts.year}`;
      return `${parts.year}-${parts.month}-${parts.day}`;
    }
    function formatTimeFromParts(parts, options = {}) {
      const includeSeconds = options.includeSeconds !== false;
      const base = includeSeconds
        ? `${parts.hour}:${parts.minute}:${parts.second}`
        : `${parts.hour}:${parts.minute}`;
      return activeClockFormat === '12h'
        ? `${base} ${parts.dayPeriod || 'AM'}`
        : base;
    }
    function fmtTime(iso, options = {}) {
      if (!iso) return '';
      const t = Date.parse(iso);
      if (!Number.isFinite(t)) return safe(iso);
      const dt = new Date(t);
      try {
        const parts = getDateTimeParts(dt, { includeSeconds: options.includeSeconds !== false });
        const dateLabel = formatDateFromParts(parts);
        if (options.includeTime === false) return dateLabel;
        const timeLabel = formatTimeFromParts(parts, { includeSeconds: options.includeSeconds !== false });
        if (options.omitDate) return timeLabel;
        return `${dateLabel} ${timeLabel}`;
      } catch (e) {
        return dt.toLocaleString();
      }
    }
    function getDisplayDateKey(value) {
      try {
        const parts = getDateTimeParts(value instanceof Date ? value : new Date(value), { includeSeconds: false });
        return `${parts.year}-${parts.month}-${parts.day}`;
      } catch (e) {
        return '';
      }
    }
    function fmtRecentActionStamp(iso) {
      if (!iso) return '';
      const t = Date.parse(iso);
      if (!Number.isFinite(t)) return safe(iso);
      const sameDay = getDisplayDateKey(new Date(t)) === getDisplayDateKey(new Date());
      return fmtTime(iso, { includeSeconds: false, omitDate: sameDay });
    }
    function appLabel(app) {
      const value = String(app || '').trim().toLowerCase();
      if (value === 'radarr') return 'Radarr';
      if (value === 'sonarr') return 'Sonarr';
      return value ? value.toUpperCase() : 'Unknown';
    }
    function actionKindMeta(kind, itemKey) {
      const raw = String(kind || '').trim().toLowerCase();
      const key = String(itemKey || '').trim().toLowerCase();
      let typeLabel = '';
      if (key.startsWith('movie:')) typeLabel = 'Movie';
      else if (key.startsWith('episode:')) typeLabel = 'Episode';
      else if (key.startsWith('season:')) typeLabel = 'Season Pack';
      else if (key.startsWith('series:')) typeLabel = 'Show Batch';
      if (raw === 'cutoff') {
        return { label: 'Upgrade', className: 'upgrade', typeLabel };
      }
      if (raw === 'monitored') {
        return { label: 'Library Upgrade', className: 'library', typeLabel };
      }
      if (raw === 'missing') {
        return { label: 'Download', className: 'download', typeLabel };
      }
      return { label: '', className: '', typeLabel };
    }
    const sectionMeta = {
      dashboard: {
        title: 'Dashboard',
        subtitle: 'Overview, schedules, and recent search activity.',
      },
      runs: {
        title: 'History',
        subtitle: 'Per-instance search history and recent activity.',
      },
      settings: {
        title: 'Configuration',
        subtitle: 'Global settings, instance controls, and automation behavior.',
      },
    };
    function syncTopbar(name) {
      const meta = sectionMeta[name] || sectionMeta.dashboard;
      const title = document.getElementById('topbar-title');
      const subtitle = document.getElementById('topbar-subtitle');
      if (title) title.textContent = meta.title;
      if (subtitle) subtitle.textContent = meta.subtitle;
    }
    function setSection(name) {
      document.querySelectorAll('.content-section').forEach(s => s.classList.remove('active'));
      document.getElementById(`section-${name}`)?.classList.add('active');
      document.querySelectorAll('.nav-control').forEach(a => a.classList.remove('active'));
      document.querySelectorAll(`.nav-control[data-section="${name}"]`).forEach(a => a.classList.add('active'));
      syncTopbar(name);
    }
