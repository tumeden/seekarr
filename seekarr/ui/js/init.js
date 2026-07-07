    document.querySelectorAll('.nav-control').forEach(a => {
      a.addEventListener('click', (e) => {
        e.preventDefault();
        setSection(a.dataset.section);
      });
    });

    document.getElementById('instance-cards').addEventListener('click', (e) => {
      const addInstanceBtn = e.target && e.target.closest ? e.target.closest('button[data-dashboard-add-instance]') : null;
      if (addInstanceBtn) {
        const app = String(addInstanceBtn.getAttribute('data-dashboard-add-instance') || '').trim().toLowerCase();
        window.settingsActiveTab = app === 'sonarr' ? 'sonarr' : 'radarr';
        setSection('settings');
        loadSettings();
        return;
      }
      const settingsBtn = e.target && e.target.closest ? e.target.closest('button[data-open-settings-app]') : null;
      if (settingsBtn) {
        const app = settingsBtn.getAttribute('data-open-settings-app');
        const id = Number(settingsBtn.getAttribute('data-open-settings-id') || 0);
        if (!app || !id) return;
        openSettingsInstance(app, id);
        return;
      }
      const btn = e.target && e.target.closest ? e.target.closest('button[data-force-app]') : null;
      if (!btn) return;
      if (btn.disabled) return;
      const app = btn.getAttribute('data-force-app');
      const id = Number(btn.getAttribute('data-force-id') || 0);
      if (!app || !id) return;
      forceRunInstance(app, id);
    });

    document.getElementById('auth-submit').addEventListener('click', authSubmit);
    document.getElementById('auth-password').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') authSubmit();
    });
    document.getElementById('delete-instance-cancel').addEventListener('click', hideDeleteInstanceModal);
    document.getElementById('delete-instance-submit').addEventListener('click', submitDeleteInstance);
    document.getElementById('delete-instance-password').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submitDeleteInstance();
      if (e.key === 'Escape') hideDeleteInstanceModal();
    });
    document.getElementById('delete-instance-modal').addEventListener('click', (e) => {
      if (e.target === e.currentTarget) hideDeleteInstanceModal();
	    });
	    document.getElementById('settings-instance-cards').addEventListener('click', async (e) => {
	      const msg = document.getElementById('settings-msg');
	      const closeInstanceSettings = (panel) => {
	        if (!panel) return;
	        panel.setAttribute('hidden', '');
	        const card = panel.closest('.settings-instance-card');
	        card?.classList.remove('is-open');
	        const btn = card?.querySelector(`button[aria-controls="${panel.id}"]`);
	        btn?.setAttribute('aria-expanded', 'false');
	        if (window.settingsOpenSettingsKey === String(card?.getAttribute('data-key') || '')) {
	          window.settingsOpenSettingsKey = '';
	        }
	      };

	      const closeBtn = e.target && e.target.closest ? e.target.closest('button[data-close-instance-settings]') : null;
	      const backdrop = e.target && e.target.classList && e.target.classList.contains('settings-instance-settings') ? e.target : null;
	      if (closeBtn || backdrop) {
	        closeInstanceSettings(closeBtn ? closeBtn.closest('.settings-instance-settings') : backdrop);
	        return;
	      }

	      const addBtn = e.target && e.target.closest ? e.target.closest('button[data-add-instance]') : null;
	      if (addBtn) {
	        const app = String(addBtn.getAttribute('data-add-instance') || '').trim().toLowerCase();
	        if (app === 'radarr' || app === 'sonarr') addSettingsInstance(app);
	        return;
	      }

	      const settingsBtn = e.target && e.target.closest ? e.target.closest('button[data-toggle-instance-settings]') : null;
	      if (settingsBtn) {
	        const card = settingsBtn.closest('.settings-instance-card');
	        const panelId = settingsBtn.getAttribute('aria-controls');
	        const panel = panelId ? document.getElementById(panelId) : null;
	        if (!card || !panel) return;
	        const key = String(card.getAttribute('data-key') || '');
	        const willOpen = panel.hasAttribute('hidden');
	        if (willOpen) {
	          document.querySelectorAll('.settings-instance-settings:not([hidden])').forEach(openPanel => {
	            if (openPanel !== panel) closeInstanceSettings(openPanel);
	          });
	        }
	        panel.toggleAttribute('hidden', !willOpen);
	        card.classList.toggle('is-open', willOpen);
	        settingsBtn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
	        window.settingsOpenSettingsKey = willOpen ? key : '';
	        return;
	      }

	      const deleteBtn = e.target && e.target.closest ? e.target.closest('button[data-delete-instance]') : null;
	      if (deleteBtn) {
        if (deleteBtn.disabled) return;
        const app = String(deleteBtn.getAttribute('data-app') || '').trim();
        const instanceId = Number(deleteBtn.getAttribute('data-id') || 0);
        const instanceName = String(deleteBtn.getAttribute('data-name') || '').trim();
        if (!app || !instanceId) return;
        showDeleteInstanceModal({
          app,
          instanceId,
          instanceName: instanceName || `#${instanceId}`,
          discardUnsaved: settingsDirty,
        });
        return;
      }

      const testBtn = e.target && e.target.closest ? e.target.closest('button[data-test-connection]') : null;
      if (testBtn) {
        if (testBtn.disabled) return;
        const card = testBtn.closest('.settings-instance-card');
        if (!card) return;
        const app = String(testBtn.getAttribute('data-app') || card.getAttribute('data-app') || '').trim();
        const instanceId = Number(testBtn.getAttribute('data-id') || card.getAttribute('data-id') || 0);
        const arrUrl = String(card.querySelector('.si_url')?.value || '').trim();
        const apiKey = String(card.querySelector('.si_apikey')?.value || '').trim();
        if (!app || !instanceId) return;
        testBtn.disabled = true;
        msg.textContent = 'Testing connection...';
        settingsStatusMessage = 'Testing connection...';
        syncSettingsSaveFab();
        try {
          const r = await apiFetch('/api/instances/test_connection', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ app, instance_id: instanceId, arr_url: arrUrl, arr_api_key: apiKey }),
          });
          const data = await r.json().catch(() => ({}));
          if (!r.ok || !data.ok) {
            const text = data.error || 'Connection test failed';
            msg.textContent = text;
            settingsStatusMessage = text;
            showToast('Connection Failed', text, 'error');
            return;
          }
          const text = data.message || 'Connection OK';
          msg.textContent = text;
          settingsStatusMessage = text;
          showToast('Connection OK', text);
        } catch (err) {
          msg.textContent = 'Connection test failed';
          settingsStatusMessage = 'Connection test failed';
          showToast('Connection Failed', 'Connection test failed', 'error');
        } finally {
          testBtn.disabled = false;
          syncSettingsSaveFab();
        }
        return;
      }

      const clearBtn = e.target && e.target.closest ? e.target.closest('button[data-clear-key]') : null;
      if (!clearBtn) return;
      if (clearBtn.disabled) return;
      const app = String(clearBtn.getAttribute('data-app') || '').trim();
      const instanceId = Number(clearBtn.getAttribute('data-id') || 0);
      if (!app || !instanceId) return;
      if (!confirmDiscardUnsavedSettings('Deleting the stored API key')) return;
      if (!confirm(`Delete the stored ${app.toUpperCase()} API key for instance #${instanceId}?`)) return;
      const r = await apiFetch('/api/credentials/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ app, instance_id: instanceId }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        msg.textContent = data.error || 'Delete failed';
        settingsStatusMessage = data.error || 'Delete failed';
        syncSettingsSaveFab();
        return;
      }
      msg.textContent = 'API key deleted';
      settingsStatusMessage = 'API key deleted';
      syncSettingsSaveFab();
      await loadSettings();
    });
    setSection('dashboard');
    ensureAuth();

	    document.getElementById('save-settings').addEventListener('click', saveSettings);
    document.getElementById('clear-media-cache')?.addEventListener('click', clearMediaCache);
    document.getElementById('section-settings').addEventListener('input', (e) => {
      const target = e.target;
      if (!target || !(target instanceof HTMLElement)) return;
      if (target.id === 'settings-history-limit') {
        const min = Number(target.getAttribute('min') || 30);
        const max = Number(target.getAttribute('max') || 5000);
        const value = Number(target.value || 240);
        const label = document.getElementById('settings-history-limit-value');
        if (label) label.textContent = String(value);
        const fill = max > min ? ((value - min) / (max - min)) * 100 : 0;
        target.style.setProperty('--range-fill', `${Math.max(0, Math.min(100, fill))}%`);
      }
      if (
        target.id === 'settings-date-format' ||
        target.id === 'settings-time-format' ||
        target.id === 'settings-quiet-timezone' ||
        target.id === 'settings-history-limit' ||
        target.id === 'settings-cache-images' ||
        target.id === 'settings-image-cache-retention-days' ||
        target.closest('#settings-instance-cards')
      ) {
        refreshSettingsDirtyState();
      }
    });
    document.getElementById('section-settings').addEventListener('change', (e) => {
      const target = e.target;
      if (!target || !(target instanceof HTMLElement)) return;
      if (target.classList.contains('si_quiet_enabled')) {
        syncSleepWindowControls(target.closest('.settings-instance-card') || document);
      }
      if (target.classList.contains('si_missing_mode')) {
        syncSmartModeTimingControls(target.closest('.settings-instance-card') || document);
      }
      if (
        target.id === 'settings-date-format' ||
        target.id === 'settings-time-format' ||
        target.id === 'settings-quiet-timezone' ||
        target.id === 'settings-history-limit' ||
        target.id === 'settings-cache-images' ||
        target.id === 'settings-image-cache-retention-days' ||
        target.closest('#settings-instance-cards')
      ) {
        refreshSettingsDirtyState();
      }
    });
    document.querySelectorAll('.nav-control').forEach(a => {
      a.addEventListener('click', () => {
        if (a.dataset.section === 'settings') loadSettings();
      });
    });
    syncSettingsSaveFab();
