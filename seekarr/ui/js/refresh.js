    async function refresh() {
      const r = await apiFetch('/api/status');
      if (r.status === 401) {
        await ensureAuth();
        return;
      }
      const data = await r.json();
      statusData = data;
      activeTimeZone = String(data?.config?.app?.quiet_hours_timezone || '').trim();
      activeDateFormat = normalizeDateFormat(data?.config?.app?.date_format);
      activeClockFormat = normalizeTimeFormat(data?.config?.app?.time_format);
      const ver = data.version || {};
      const versionChip = document.getElementById('version-chip');
      if (versionChip) {
        versionChip.textContent = `Version ${safe(ver.current || '-')}`;
      }
      const updateChip = document.getElementById('update-chip');
      if (updateChip) {
        if (ver.update_available) {
          updateChip.style.display = 'inline';
          updateChip.href = String(ver.release_url || 'https://github.com/tumeden/seekarr/releases/latest');
          updateChip.title = ver.latest ? `Latest: ${ver.latest}` : 'Update available';
        } else {
          updateChip.style.display = 'none';
        }
      }
      
      const rs = data.run_state || {};
      updateRunStatusPill(rs);

      const hb = data.scheduler_heartbeat || null;
      const hbMs = hb ? Date.parse(hb) : NaN;
      const alive = Number.isFinite(hbMs) && (Date.now() - hbMs) < 120000;
      // We don't surface "scheduler online" as a top-level badge, but we still use it for notes.

      const syncMap = {};
      for (const s of data.sync_status || []) {
        syncMap[`${s.app_type}:${s.instance_id}`] = s;
      }



      const instances = Array.isArray(data.config?.instances) ? data.config.instances : [];
      const dashboardInstances = instances.filter(inst =>
        !!inst.enabled &&
        !!String(inst.arr_url || '').trim() &&
        !!inst.api_key_set
      );
      const cards = document.getElementById('instance-cards');
      cards.setAttribute('data-count', String(dashboardInstances.length));
      cards.innerHTML = '';
      if (!instances.length) {
        cards.innerHTML = `<div class="card empty-state-card"><div class="section-head"><h3>No Instances Configured</h3><div class="subline">Add a Radarr or Sonarr instance from Configuration.</div></div></div>`;
      } else if (!dashboardInstances.length) {
        cards.innerHTML = `<div class="card empty-state-card"><div class="section-head"><h3>No Ready Instances</h3><div class="subline">Enable an instance and add its Arr URL and API key from Configuration.</div></div></div>`;
      }
      for (const i of dashboardInstances) {
        const key = `${i.app}:${i.instance_id}`;
        const s = syncMap[key] || {};
        const used = Number((data.rate_status?.[key]?.used) ?? 0);
        const cap = Number(i.rate_cap ?? 0);
        const remaining = Math.max(0, cap - used);
        const lr = (data.instance_last_run && data.instance_last_run[key]) ? data.instance_last_run[key] : null;
        const lrs = lr && lr.stats ? lr.stats : {};
        const cd = fmtCountdown(s.next_sync_time);
        const due = (cd === 'DUE');
        const runningThis =
          !!rs.running &&
          rs.active_app_type === i.app &&
          Number(rs.active_instance_id) === Number(i.instance_id);

        let statusText = 'Waiting';
        let statusClass = 'waiting';
        if (!i.enabled) {
          statusText = 'Off';
          statusClass = 'off';
        } else if (runningThis) {
          statusText = 'Running';
          statusClass = 'running';
        } else if (due) {
          statusText = 'Due';
          statusClass = 'due';
        }

        let note = 'Scheduled';
        if (due) {
          if (!alive) note = 'Due, but scheduler is OFF';

          else note = 'Due, will run on the next scheduler tick';
        }
        const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
        const barClass = (used >= cap && cap > 0) ? 'bar cap' : 'bar';
        const canForce = !!i.enabled;
        const disabledAttr = (!canForce || !!rs.running) ? 'disabled' : '';
        const runTitle = runningThis ? 'Run in progress' : (canForce ? 'Run now' : 'Run unavailable');
        const statusHtml = statusClass === 'waiting' ? '' : `<span class="status ${statusClass}">${statusText}</span>`;
        const safeUrl = i.arr_url ? safe(i.arr_url) : 'URL not set';
        const normalizedUrl = normalizeExternalUrl(i.arr_url);
        const dashboardUrlHtml = normalizedUrl
          ? `<a class="instance-link mono" href="${safe(normalizedUrl)}" target="_blank" rel="noopener noreferrer">${safeUrl}</a>`
          : `<span class="mono">${safeUrl}</span>`;
        cards.innerHTML += `
          <div class="instance-card instance-card-shell" data-app="${safe(i.app)}">
            <div>
              <div class="instance-head">
                <div class="instance-main">
                  <div class="instance-eyebrow">
                    <svg class="instance-eyebrow-icon" width="16" height="16" fill="none" stroke="var(--accent-color)" stroke-width="2" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
                    <span>${safe(i.app).toUpperCase()}</span>
                  </div>
                  <div class="instance-title">
                    <span class="instance-name">${safe(i.instance_name)}</span>
                    <span class="instance-id">#${safe(i.instance_id)}</span>
                  </div>
                  <div class="instance-meta">
                    ${dashboardUrlHtml}
                  </div>
                </div>
                <div class="instance-utility">
                  ${statusHtml}
                  <div class="instance-control-row">
                    <button class="card-icon-btn" data-open-settings-app="${safe(i.app)}" data-open-settings-id="${safe(i.instance_id)}" type="button" title="Settings" aria-label="Settings">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
                    </button>
                    <button class="card-icon-btn card-icon-btn-run" data-force-app="${safe(i.app)}" data-force-id="${safe(i.instance_id)}" ${disabledAttr} type="button" title="${runTitle}" aria-label="${runTitle}">
                      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M5 7.5c0-1.1 1.2-1.77 2.13-1.2l5.74 3.5c.9.55.9 1.85 0 2.4l-5.74 3.5C6.2 16.27 5 15.6 5 14.5v-7z"></path><path d="M12 7.5c0-1.1 1.2-1.77 2.13-1.2l5.74 3.5c.9.55.9 1.85 0 2.4l-5.74 3.5C13.2 16.27 12 15.6 12 14.5v-7z"></path></svg>
                    </button>
                  </div>
                </div>
              </div>
              <div class="countdown-block">
                <div>
                  <div class="big-countdown ${due ? 'due' : ''}" data-next-sync="${safe(s.next_sync_time)}">${cd}</div>
                  <div class="subline mono countdown-meta" title="${safe(s.next_sync_time) || ''}">Next run (${safe(getTimeZoneLabel())}): ${fmtTime(s.next_sync_time) || '-'}</div>
                  <div class="subline countdown-note ${due ? 'warn' : ''}">${note}</div>
                </div>
              </div>
              <div class="rate-panel">
                <div class="subline rate-row">
                  <span>Rate window (${safe(i.rate_window_minutes)}m)</span>
                  <span class="mono rate-value">${used} / ${cap}</span>
                </div>
                <div class="progress progress-slim">
                  <div class="${barClass}" style="width:${pct}%;"></div>
                </div>
              </div>
            </div>
            <div class="instance-metrics">
              <div class="kv metrics-grid">
                <div><div class="k">Wanted</div><div class="v text-strong">${safe(lrs.wanted_count ?? '-')}</div></div>
                <div><div class="k">Triggered</div><div class="v text-success text-strong">${safe(lrs.actions_triggered ?? '-')}</div></div>
                <div><div class="k">Interval</div><div class="v">${safe(i.interval_minutes)}m</div></div>
                <div><div class="k">Retry</div><div class="v">${safe(i.item_retry_hours)}h</div></div>
                <div><div class="k k-nowrap">Last Sync</div><div class="v mono metric-time">${fmtTime(s.last_sync_time) || '-'}</div></div>
                <div><div class="k">Window</div><div class="v">${safe(i.rate_window_minutes)}m</div></div>
              </div>
            </div>
          </div>
        `;
      }

      renderHistorySection(data, instances);

      const actionsEl = document.getElementById('recent-actions');
      const instanceNameMap = new Map(
        instances.map(inst => [`${String(inst.app || '').toLowerCase()}:${Number(inst.instance_id || 0)}`, String(inst.instance_name || '').trim()])
      );
      const actions = Array.isArray(data.recent_actions)
        ? data.recent_actions.map(a => ({
            ts: a.occurred_at,
            app_type: a.app_type,
            instance_id: a.instance_id,
            instance_name: a.instance_name,
            item_key: a.item_key,
            action_kind: a.action_kind,
            item_url: a.item_url,
            cover_url: a.cover_url,
            title: a.title,
          }))
        : (Array.isArray(rs.recent_actions) ? rs.recent_actions : []);
      const recentActionsRenderKey = JSON.stringify(actions.slice(0, 12).map(a => [
        a.ts || '',
        a.app_type || '',
        a.instance_id || '',
        a.instance_name || '',
        a.item_key || '',
        a.action_kind || '',
        a.item_url || '',
        a.cover_url || '',
        a.title || '',
      ]));
      if (recentActionsRenderKey === lastRecentActionsRenderKey && actionsEl.innerHTML) {
        tickCountdowns();
        return;
      }
      lastRecentActionsRenderKey = recentActionsRenderKey;
      if (!actions.length) {
        actionsEl.innerHTML = '<div class="recent-actions-empty">No recent searches recorded yet.</div>';
      } else {
        actionsEl.innerHTML = actions.slice(0, 12).map(a => {
          const appType = String(a.app_type || '').trim().toLowerCase();
          const instanceId = Number(a.instance_id || 0);
          const currentInstanceName = instanceNameMap.get(`${appType}:${instanceId}`) || String(a.instance_name || '').trim();
          const sourceLabel = currentInstanceName ? `${appLabel(appType)} / ${currentInstanceName}` : appLabel(appType);
          const kindMeta = actionKindMeta(a.action_kind, a.item_key);
          const itemUrl = String(a.item_url || '').trim();
          const coverUrl = String(a.cover_url || '').trim();
          const itemOpenArgs = `${JSON.stringify(appType)}, ${JSON.stringify(String(instanceId))}, ${JSON.stringify(String(a.item_key || ''))}, ${JSON.stringify(itemUrl)}`;
          const rowClass = a.item_key ? 'recent-action-row' : 'recent-action-row no-cover';
          const coverWrapClass = (a.item_key && coverUrl) ? '' : ' is-empty';
          const coverHtml = coverUrl
            ? `<img class="recent-action-cover" src="${safe(coverUrl)}" alt="" loading="eager" fetchpriority="high" decoding="async">`
            : '';
          const buttonAttrs = itemUrl ? ` data-item-url="${safe(itemUrl)}"` : '';
          return `
            <div class="${rowClass}" data-app="${safe(appType)}" data-instance-id="${safe(String(instanceId))}" data-item-key="${safe(String(a.item_key || ''))}" data-cover-url="${safe(coverUrl)}">
              <div class="recent-action-cover-wrap${coverWrapClass}">${coverHtml}</div>
              <div class="recent-action-time mono" title="${safe(fmtTime(a.ts) || '')}">${safe(fmtRecentActionStamp(a.ts) || '--')}</div>
              <div class="recent-action-main">
                <button class="recent-action-title recent-action-link" type="button"${buttonAttrs} onclick='openRecentActionItem(${itemOpenArgs}); return false;'>${safe(a.title || 'Untitled search')}</button>
                <div class="recent-action-meta">${renderActionMetaBadges(kindMeta, sourceLabel)}</div>
              </div>
            </div>
          `;
        }).join('');
      }
      settleActionCoverPaint(actionsEl);
      hydrateActionMediaRows(actionsEl);
      tickCountdowns();
    }
