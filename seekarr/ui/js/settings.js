    function sortSettingsInstances(instances) {
      return [...(instances || [])].sort((a, b) => {
        const appA = String(a.app || '');
        const appB = String(b.app || '');
        if (appA !== appB) return appA.localeCompare(appB);
        return Number(a.instance_id || 0) - Number(b.instance_id || 0);
      });
    }
    function nextSettingsInstanceId(app) {
      let maxId = 0;
      (window.settingsInstances || []).forEach(inst => {
        if (String(inst.app || '').trim().toLowerCase() !== app) return;
        maxId = Math.max(maxId, Number(inst.instance_id || 0));
      });
      return maxId + 1;
    }
    function newSettingsInstance(app) {
      const instanceId = nextSettingsInstanceId(app);
      const label = app === 'radarr' ? 'Radarr' : 'Sonarr';
      return {
        app,
        instance_id: instanceId,
        instance_name: `${label} ${instanceId}`,
        enabled: false,
        interval_minutes: 15,
        search_missing: true,
        search_cutoff_unmet: true,
        upgrade_scope: 'wanted',
        search_order: 'smart',
        quiet_hours_enabled: true,
        quiet_hours_start: '23:00',
        quiet_hours_end: '06:00',
        min_hours_after_release: 8,
        min_seconds_between_actions: 2,
        max_missing_actions_per_instance_per_sync: 5,
        max_cutoff_actions_per_instance_per_sync: 1,
        sonarr_missing_mode: 'smart',
        item_retry_hours: 72,
        rate_window_minutes: 60,
        rate_cap: 25,
        arr_url: '',
        api_key_set: false,
      };
    }
    function applySettingsNavigationTarget(instances) {
      const target = window.settingsNavigationTarget;
      if (!target) return;
      const app = String(target.app || '').trim().toLowerCase();
      const instanceId = Number(target.instance_id || 0);
      const openModal = target.open_modal !== false;
      if ((app !== 'radarr' && app !== 'sonarr') || !instanceId) {
        window.settingsNavigationTarget = null;
        return;
      }
      const match = (instances || []).find(inst =>
        String(inst.app || '').trim().toLowerCase() === app &&
        Number(inst.instance_id || 0) === instanceId
      );
      if (!match) {
        window.settingsNavigationTarget = null;
        return;
      }
      window.settingsActiveTab = app;
      window.settingsOpenSettingsKey = openModal ? `${app}:${instanceId}` : '';
      window.settingsNavigationTarget = null;
      requestAnimationFrame(() => {
        const card = document.querySelector(`#settings-instance-cards .settings-instance-card[data-key="${app}:${instanceId}"]`);
        card?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
    window.openSettingsInstance = async function(app, instanceId, options = {}) {
      const normalizedApp = String(app || '').trim().toLowerCase();
      const normalizedInstanceId = Number(instanceId || 0);
      if ((normalizedApp !== 'radarr' && normalizedApp !== 'sonarr') || !normalizedInstanceId) return;
      window.settingsNavigationTarget = {
        app: normalizedApp,
        instance_id: normalizedInstanceId,
        open_modal: options.openModal !== false,
      };
      setSection('settings');
      await loadSettings();
    };
    window.updateSettingsTabs = function(instances) {
      const tabsWrap = document.getElementById('settings-tabs');
      if (!tabsWrap) return;
      const tabs = ['global', 'radarr', 'sonarr'];
      if (!tabs.includes(window.settingsActiveTab)) window.settingsActiveTab = 'global';

      const counts = {
        radarr: (instances || []).filter(inst => String(inst.app || '').toLowerCase() === 'radarr').length,
        sonarr: (instances || []).filter(inst => String(inst.app || '').toLowerCase() === 'sonarr').length,
      };
      const tabButton = (key, label) => {
        const suffix = counts[key] ? ` (${counts[key]})` : '';
        const active = window.settingsActiveTab === key ? 'active' : '';
        return `<button class="tab-btn settings-tab-btn ${active}" onclick="window.settingsActiveTab='${key}'; window.updateSettingsTabs(window.settingsInstances); return false;">${label}${suffix}</button>`;
      };

      tabsWrap.innerHTML = [
        `<button class="tab-btn settings-tab-btn ${window.settingsActiveTab === 'global' ? 'active' : ''}" onclick="window.settingsActiveTab='global'; window.updateSettingsTabs(window.settingsInstances); return false;">Global Settings</button>`,
        tabButton('radarr', 'Radarr'),
        tabButton('sonarr', 'Sonarr'),
      ].join('');

      document.querySelectorAll('.settings-tab-content').forEach(el => {
        el.style.display = (el.id === `settings-tab-${window.settingsActiveTab}`) ? 'block' : 'none';
      });
    };
    function renderSettingsCards(instances) {
      const wrap = document.getElementById('settings-instance-cards');
      wrap.innerHTML = '';
      window.settingsInstances = sortSettingsInstances(instances);

      function renderAddTile(app) {
        const label = app === 'radarr' ? 'Radarr' : 'Sonarr';
        const sub = app === 'radarr' ? 'Add a movie server' : 'Add a series server';
        return `
          <button class="settings-add-tile settings-add-server-card" data-add-instance="${app}" type="button">
            <span class="settings-add-tile-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"></path><path d="M5 12h14"></path></svg>
            </span>
            <span class="settings-add-tile-copy">
              <span class="settings-add-tile-title">Add ${label}</span>
              <span class="settings-add-tile-sub">${sub}</span>
            </span>
          </button>
        `;
      }

      function renderInstanceCard(inst) {
        const key = `${inst.app}:${inst.instance_id}`;
        const detailId = `settings-detail-${safe(inst.app)}-${safe(inst.instance_id)}`;
        const settingsOpen = window.settingsOpenSettingsKey === key;
        const instanceName = String(inst.instance_name || `${String(inst.app || '').toUpperCase()} ${inst.instance_id}`).trim();
        const mode = String(inst.sonarr_missing_mode || 'smart').toLowerCase();
        const upgradeScopeRaw = String(inst.upgrade_scope || 'wanted').toLowerCase();
        const upgradeScope = (upgradeScopeRaw === 'all_monitored') ? 'both' : upgradeScopeRaw;
        const order = String(inst.search_order || 'smart').toLowerCase();
        const sleepEnabled = (inst.quiet_hours_enabled !== false);
        const modeUi = (inst.app === 'sonarr') ? `
              <div class="field field-stack-gap">
                <div class="label">
                  Missing Mode
                  <span class="info-icon" title="Smart auto-selects the best mode. Season Packs uses season searches. Show Batch searches all missing episodes in a show. Episode searches one episode at a time."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <select class="cfg si_missing_mode" name="settings_${safe(key)}_missing_mode">
                  <option value="smart" ${mode === 'smart' ? 'selected' : ''}>Smart</option>
                  <option value="season_packs" ${mode === 'season_packs' ? 'selected' : ''}>Season Packs</option>
                  <option value="shows" ${mode === 'shows' ? 'selected' : ''}>Show Batch</option>
                  <option value="episodes" ${mode === 'episodes' ? 'selected' : ''}>Episode</option>
                </select>
              </div>
        ` : '';
        const runScheduleUi = `
            <div class="settings-grid-auto">
              <div class="field">
                <div class="label">
                  Run Every (min)
                  <span class="info-icon" title="How often Seekarr should check this instance on its normal schedule."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg si_interval" name="settings_${safe(key)}_interval" type="number" min="1" value="${safe(inst.interval_minutes)}"/>
              </div>
            </div>
        `;
        const sleepWindowUi = `
            <div class="settings-grid-auto">
              <div class="field field-stack-gap">
                <div class="settings-switch-row">
                  <label class="settings-switch">
                    <input type="checkbox" class="si_quiet_enabled" name="settings_${safe(key)}_quiet_enabled" ${sleepEnabled ? 'checked' : ''}>
                    <span class="settings-switch-slider" aria-hidden="true"></span>
                    <span class="settings-switch-label">Enabled</span>
                  </label>
                  <span class="info-icon" title="When enabled, this instance will not run during the sleep window. Force runs still bypass it."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <div class="subline">Blocks scheduled runs between the start and end times below. Uses the global timezone configured above.</div>
              </div>
            </div>
            <div class="settings-grid-auto settings-grid-spaced sleep-window-fields${sleepEnabled ? '' : ' is-disabled'}">
              <div class="field">
                <div class="label">Start (HH:MM)</div>
                <input class="cfg mono si_quiet_start" name="settings_${safe(key)}_quiet_start" type="text" value="${safe(inst.quiet_hours_start)}" placeholder="23:00" ${sleepEnabled ? '' : 'disabled'} aria-disabled="${sleepEnabled ? 'false' : 'true'}"/>
              </div>
              <div class="field">
                <div class="label">End (HH:MM)</div>
                <input class="cfg mono si_quiet_end" name="settings_${safe(key)}_quiet_end" type="text" value="${safe(inst.quiet_hours_end)}" placeholder="06:00" ${sleepEnabled ? '' : 'disabled'} aria-disabled="${sleepEnabled ? 'false' : 'true'}"/>
              </div>
            </div>
        `;
        const orderUi = `
              <div class="field">
                <div class="label">Search Order</div>
                  <select class="cfg si_search_order" name="settings_${safe(key)}_search_order">
                  <option value="smart" ${order === 'smart' ? 'selected' : ''}>Smart (Recent, Random, Oldest)</option>
                  <option value="newest" ${order === 'newest' ? 'selected' : ''}>Newest First</option>
                  <option value="random" ${order === 'random' ? 'selected' : ''}>Random</option>
                  <option value="oldest" ${order === 'oldest' ? 'selected' : ''}>Oldest First</option>
                </select>
              </div>
        `;
        const behaviorUi = `
            <div class="settings-grid-auto settings-grid-spaced">
              <div class="field">
                <div class="label">
                  Upgrade Source
                  <span class="info-icon" title="Wanted List uses the Arr upgrade queue. Monitored Items checks monitored items with existing files. Both combines both sources."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <select class="cfg si_upgrade_scope" name="settings_${safe(key)}_upgrade_scope">
                  <option value="wanted" ${upgradeScope === 'wanted' ? 'selected' : ''}>Wanted List</option>
                  <option value="monitored" ${upgradeScope === 'monitored' ? 'selected' : ''}>Monitored Items</option>
                  <option value="both" ${upgradeScope === 'both' ? 'selected' : ''}>Both Sources</option>
                </select>
              </div>
              ${orderUi}
              <div class="field">
                <div class="label">Missing Per Run</div>
                <input class="cfg si_missing_per_run" name="settings_${safe(key)}_missing_per_run" type="number" min="0" value="${safe(inst.max_missing_actions_per_instance_per_sync)}"/>
              </div>
              <div class="field">
                <div class="label">Upgrades Per Run</div>
                <input class="cfg si_upgrades_per_run" name="settings_${safe(key)}_upgrades_per_run" type="number" min="0" value="${safe(inst.max_cutoff_actions_per_instance_per_sync)}"/>
              </div>
            </div>
            <div class="settings-grid-auto">
              ${modeUi}
            </div>
        `;
        const timingUi = `
            <div class="settings-grid-auto">
              <div class="field">
                <div class="label">
                  Hours After Release
                  <span class="info-icon" title="Minimum hours after a title's release date before Seekarr will search for it."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg si_after_release" name="settings_${safe(key)}_after_release" type="number" min="0" value="${safe(inst.min_hours_after_release)}"/>
              </div>
              <div class="field">
                <div class="label">Retry (hours)</div>
                <input class="cfg si_retry" name="settings_${safe(key)}_retry_hours" type="number" min="1" value="${safe(inst.item_retry_hours)}"/>
              </div>
              <div class="field">
                <div class="label">
                  Seconds Between
                  <span class="info-icon" title="Minimum delay in seconds between consecutive search actions to avoid hammering the indexer."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg si_between" name="settings_${safe(key)}_seconds_between" type="number" min="0" value="${safe(inst.min_seconds_between_actions)}"/>
              </div>
              <div class="field">
                <div class="label">Rate Window (min)</div>
                <input class="cfg si_rate_window" name="settings_${safe(key)}_rate_window" type="number" min="1" value="${safe(inst.rate_window_minutes)}"/>
              </div>
              <div class="field">
                <div class="label">Rate Cap</div>
                <input class="cfg si_rate_cap" name="settings_${safe(key)}_rate_cap" type="number" min="1" value="${safe(inst.rate_cap)}"/>
              </div>
            </div>
        `;

        return `
          <div class="settings-server-card settings-instance-card ${settingsOpen ? 'is-open' : ''}" data-app="${safe(inst.app)}" data-id="${safe(inst.instance_id)}" data-key="${safe(key)}">
            <div class="settings-server-head">
              <div class="settings-server-summary">
                <div class="settings-server-title">
                  <svg class="settings-instance-icon" width="22" height="22" fill="none" stroke="var(--accent-color)" stroke-width="2" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
                  <span>${safe(instanceName)}</span>
                  <span class="settings-instance-badge">#${safe(inst.instance_id)}</span>
                </div>
                <div class="subline mono settings-instance-url settings-server-url">${
                  inst.arr_url
                    ? `<a class="settings-instance-link" href="${safe(normalizeExternalUrl(inst.arr_url))}" target="_blank" rel="noopener noreferrer">${safe(inst.arr_url)}</a>`
                    : 'No URL configured'
                }</div>
              </div>
	              <div class="settings-server-actions">
	                <label class="settings-switch settings-card-toggle">
	                  <input type="checkbox" class="si_enabled" name="settings_${safe(key)}_enabled" ${inst.enabled ? 'checked' : ''}>
	                  <span class="settings-switch-slider" aria-hidden="true"></span>
	                  <span class="settings-switch-label">Enabled</span>
	                </label>
	                <button class="btn-secondary settings-card-action" type="button" data-toggle-instance-settings="1" aria-expanded="${settingsOpen ? 'true' : 'false'}" aria-controls="${detailId}">
	                  Settings
	                </button>
                <button
                  class="btn-secondary danger-soft"
                  type="button"
                  data-delete-instance="1"
                  data-app="${safe(inst.app)}"
                  data-id="${safe(inst.instance_id)}"
                  data-name="${safe(instanceName)}"
                >
                  Delete
                </button>
              </div>
            </div>

            <div class="settings-connection-grid">
              <div class="field">
                <div class="label">Instance Name</div>
                <input class="cfg si_name" name="settings_${safe(key)}_name" type="text" value="${safe(instanceName)}"/>
              </div>
              <div class="field">
                <div class="label">Arr URL</div>
                <input class="cfg mono si_url" name="settings_${safe(key)}_url" type="text" value="${safe(inst.arr_url)}"/>
              </div>
              <div class="field">
                <div class="label">
                  API Key
                  <span class="info-icon" title="Enter a new key to update it. Leave blank to keep the existing key unchanged."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <div class="inline-input settings-api-key-row">
                  <input class="cfg mono si_apikey" name="settings_${safe(key)}_api_key" type="password" value="" placeholder="${inst.api_key_set ? '********' : '(not set)'}"/>
                  <button class="icon-btn settings-test-connection-btn" type="button" title="Test connection"
                          data-test-connection="1" data-app="${safe(inst.app)}" data-id="${safe(inst.instance_id)}">
                    Test
                  </button>
                  <button class="icon-btn danger" type="button" title="Delete stored API key"
                          data-clear-key="1" data-app="${safe(inst.app)}" data-id="${safe(inst.instance_id)}" ${inst.api_key_set ? '' : 'disabled'}>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16">
                      <path d="M3 6h18"></path>
                      <path d="M8 6V4h8v2"></path>
                      <path d="M6 6l1 16h10l1-16"></path>
                      <path d="M10 11v6"></path>
                      <path d="M14 11v6"></path>
                    </svg>
                  </button>
                </div>
              </div>
            </div>

            <div class="settings-instance-settings" id="${detailId}" ${settingsOpen ? '' : 'hidden'}>
              <div class="settings-instance-settings-card" role="dialog" aria-modal="true" aria-labelledby="${detailId}-title">
                <div class="settings-instance-settings-head">
                  <div>
                    <h3 id="${detailId}-title">${safe(instanceName)} Settings</h3>
                    <div class="subline">${safe(inst.app).toUpperCase()} instance #${safe(inst.instance_id)}</div>
                  </div>
                  <button class="icon-btn settings-modal-close" type="button" title="Close settings" data-close-instance-settings="1">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" width="16" height="16" aria-hidden="true">
                      <path d="M18 6 6 18"></path>
                      <path d="m6 6 12 12"></path>
                    </svg>
                  </button>
                </div>
	              <div class="settings-panel">
	                <div class="settings-panel-copy">
	                  <h4 class="settings-panel-title">Search Types</h4>
	                  <div class="subline settings-panel-help">Choose which search types this instance can run.</div>
	                </div>
	                <div class="settings-panel-body">
	                  <div class="pill-row settings-toggle-group">
	                    <label class="tog subline settings-toggle-chip"><input type="checkbox" class="si_missing" name="settings_${safe(key)}_missing" ${inst.search_missing ? 'checked' : ''}> Missing</label>
	                    <label class="tog subline settings-toggle-chip"><input type="checkbox" class="si_cutoff" name="settings_${safe(key)}_cutoff" ${inst.search_cutoff_unmet ? 'checked' : ''}> Upgrades</label>
	                  </div>
                </div>
              </div>

              <div class="settings-panel">
                <div class="settings-panel-copy">
                  <h4 class="settings-panel-title">Run Schedule</h4>
                  <div class="subline settings-panel-help">How often Seekarr checks this instance automatically.</div>
                </div>
                <div class="settings-panel-body">${runScheduleUi}</div>
              </div>

              <div class="settings-panel">
                <div class="settings-panel-copy">
                  <h4 class="settings-panel-title">Sleep Window</h4>
                  <div class="subline settings-panel-help">Pause scheduled runs during quiet hours.</div>
                </div>
                <div class="settings-panel-body">${sleepWindowUi}</div>
              </div>

              <div class="settings-panel">
                <div class="settings-panel-copy">
                  <h4 class="settings-panel-title">Search Behavior</h4>
                  <div class="subline settings-panel-help">Choose what Seekarr searches and how many actions it can take.</div>
                </div>
                <div class="settings-panel-body">${behaviorUi}</div>
              </div>

              <div class="settings-panel">
                <div class="settings-panel-copy">
                  <h4 class="settings-panel-title">Search Timing & Rate Limits</h4>
                  <div class="subline settings-panel-help">Control release delay, retries, and request pacing.</div>
                </div>
                <div class="settings-panel-body">${timingUi}</div>
              </div>
              </div>
            </div>
          </div>
        `;
      }

      function renderAppTab(app) {
        const label = app === 'radarr' ? 'Radarr' : 'Sonarr';
        const itemLabel = app === 'radarr' ? 'movie' : 'series';
        const appInstances = window.settingsInstances.filter(inst => String(inst.app || '').toLowerCase() === app);
        return `
          <div class="card settings-tab-content settings-app-card" id="settings-tab-${app}">
            <div class="settings-app-head">
              <div>
                <h3>${label} Settings</h3>
                <div class="subline">${label} connection instances used for ${itemLabel} searches.</div>
              </div>
            </div>
            <div class="settings-server-grid">
              ${appInstances.map(renderInstanceCard).join('')}
              ${renderAddTile(app)}
            </div>
          </div>
        `;
      }

      wrap.innerHTML = renderAppTab('radarr') + renderAppTab('sonarr');
      syncSleepWindowControls(wrap);
      window.updateSettingsTabs(window.settingsInstances);
    }

    function addSettingsInstance(app) {
      const next = newSettingsInstance(app);
      window.settingsInstances = sortSettingsInstances([...(window.settingsInstances || []), next]);
      window.settingsActiveTab = next.app;
      window.settingsOpenSettingsKey = '';
      renderSettingsCards(window.settingsInstances);
      refreshSettingsDirtyState();
    }

    function syncHistoryLimitLabel() {
      const input = document.getElementById('settings-history-limit');
      const label = document.getElementById('settings-history-limit-value');
      if (!input) return;
      const value = Number(input.value || 240);
      if (label) label.textContent = String(value);
      const min = Number(input.min || 30);
      const max = Number(input.max || 5000);
      const fill = max > min ? ((value - min) / (max - min)) * 100 : 0;
      input.style.setProperty('--range-fill', `${Math.max(0, Math.min(100, fill))}%`);
    }

    async function loadSettings() {
      settingsLoaded = false;
      populateTimezoneOptions();
      const r = await apiFetch('/api/settings', { cache:'no-store' });
      const data = await r.json();
      const appCfg = data.app || {};
      document.getElementById('settings-quiet-timezone').value = String(appCfg.quiet_hours_timezone || '').trim();
      document.getElementById('settings-date-format').value = normalizeDateFormat(appCfg.date_format);
      document.getElementById('settings-time-format').value = normalizeTimeFormat(appCfg.time_format);
      document.getElementById('settings-history-limit').value = String(Number(appCfg.history_limit || 240));
      syncHistoryLimitLabel();
      document.getElementById('settings-cache-images').checked = appCfg.cache_images === true;
      document.getElementById('settings-image-cache-retention-days').value = String(Number(appCfg.image_cache_retention_days || 30));
      const cacheStats = appCfg.media_cache || {};
      const cacheStatsEl = document.getElementById('settings-media-cache-stats');
      if (cacheStatsEl) {
        cacheStatsEl.textContent = `${Number(cacheStats.files || 0)} files / ${fmtBytes(cacheStats.bytes || 0)}`;
      }
      applySettingsNavigationTarget(data.instances || []);
      renderSettingsCards(data.instances || []);
      settingsBaseline = settingsPayloadFingerprint(buildSettingsPayload());
      settingsLoaded = true;
      setSettingsDirtyState(false, '');
    }

    async function clearMediaCache() {
      const btn = document.getElementById('clear-media-cache');
      const statsEl = document.getElementById('settings-media-cache-stats');
      if (!btn) return;
      btn.disabled = true;
      const oldText = btn.textContent;
      btn.textContent = 'Clearing...';
      try {
        const r = await apiFetch('/api/media_cache/clear', { method: 'POST' });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.ok) throw new Error(data.error || 'Failed to clear image cache');
        const stats = data.media_cache || {};
        if (statsEl) {
          statsEl.textContent = `${Number(stats.files || 0)} files / ${fmtBytes(stats.bytes || 0)}`;
        }
        showToast('Image Cache Cleared', `Removed ${Number(data.files_removed || 0)} files. Backfill will repopulate cached posters in the background.`);
        if (typeof refresh === 'function') await refresh();
      } catch (err) {
        showToast('Clear Failed', err?.message || 'Failed to clear image cache', 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = oldText || 'Clear Cache';
      }
    }

    async function saveSettings() {
      const msg = document.getElementById('settings-msg');
      const btn = document.getElementById('save-settings');
      btn.disabled = true;
      settingsStatusMessage = 'Saving...';
      syncSettingsSaveFab();

      try {
        if (!settingsLoaded) {
          await loadSettings();
          msg.textContent = 'Settings loaded. Review changes and save again.';
          settingsStatusMessage = msg.textContent;
          return;
        }
        const payload = buildSettingsPayload();
        const instances = payload.instances;
        const invalidInstance = instances.find(inst => !inst.instance_name);
        if (invalidInstance) {
          msg.textContent = `Instance #${invalidInstance.instance_id} needs a name`;
          settingsStatusMessage = msg.textContent;
          return;
        }

        const r = await apiFetch('/api/settings', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify(payload),
        });
        const data = await r.json();
        if (!r.ok) {
          msg.textContent = data.error || 'Save failed';
          settingsStatusMessage = msg.textContent;
          return;
        }
        msg.textContent = 'Saved';
        await loadSettings();
        await refresh();
      } catch (e) {
        msg.textContent = 'Save failed';
        settingsStatusMessage = 'Save failed';
      } finally {
        btn.disabled = false;
        syncSettingsSaveFab();
      }
    }
