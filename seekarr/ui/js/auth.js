    function populateTimezoneOptions() {
      if (timezoneOptionsLoaded) return;
      timezoneOptionsLoaded = true;
      const dl = document.getElementById('timezone-options');
      if (!dl) return;
      let zones = [];
      try {
        if (Intl && typeof Intl.supportedValuesOf === 'function') {
          zones = Intl.supportedValuesOf('timeZone') || [];
        }
      } catch (e) {}
      if (!zones.length) zones = timezoneFallback.slice();
      zones = Array.from(new Set([...zones, ...timezoneFallback])).sort((a, b) => a.localeCompare(b));
      const frag = document.createDocumentFragment();
      for (const z of zones) {
        const o = document.createElement('option');
        o.value = z;
        frag.appendChild(o);
      }
      dl.appendChild(frag);
    }
    function startTimers() {
      if (timersStarted) return;
      timersStarted = true;
      refreshTimer = setInterval(refresh, 5000);
      countdownTimer = setInterval(tickCountdowns, 1000);
    }

    function loadAuthHeader() {
      try {
        const v = localStorage.getItem(authStorageKey);
        authHeader = (v && typeof v === 'string') ? v : '';
      } catch (e) {
        authHeader = '';
      }
    }

    function saveAuthHeader() {
      try {
        if (authHeader) localStorage.setItem(authStorageKey, authHeader);
      } catch (e) {}
    }

    function clearAuthHeader() {
      authHeader = '';
      try {
        localStorage.removeItem(authStorageKey);
      } catch (e) {}
    }

    function apiFetch(url, opts) {
      const o = opts ? Object.assign({}, opts) : {};
      o.headers = o.headers ? Object.assign({}, o.headers) : {};
      if (authHeader) o.headers['Authorization'] = authHeader;
      if (!('cache' in o)) o.cache = 'no-store';
      return fetch(url, o);
    }

    function showAuthModal(mode) {
      const modal = document.getElementById('auth-modal');
      const sub = document.getElementById('auth-sub');
      const label = document.getElementById('auth-label');
      const hint = document.getElementById('auth-hint');
      const err = document.getElementById('auth-error');
      const pw = document.getElementById('auth-password');
      const btn = document.getElementById('auth-submit');

      err.textContent = '';
      btn.disabled = false;

      const isShown = modal.classList.contains('show');
      const modeChanged = (authMode !== mode);
      authMode = mode;
      if (!isShown || modeChanged) {
        pw.value = '';
      }

      if (mode === 'set') {
        sub.textContent = 'Create a password to secure access to the Seekarr Web UI.';
        label.textContent = 'New Password';
        pw.setAttribute('autocomplete', 'new-password');
        hint.textContent = 'Minimum 8 characters. Saved as a salted hash in the SQLite DB.';
        btn.textContent = 'Save Password';
      } else {
        sub.textContent = 'Enter your Web UI password to continue.';
        label.textContent = 'Password';
        pw.setAttribute('autocomplete', 'current-password');
        hint.textContent = '';
        btn.textContent = 'Unlock';
      }

      document.body.classList.add('auth-locked');
      modal.classList.add('show');
      setTimeout(() => pw.focus(), 50);
    }

    function hideAuthModal() {
      document.getElementById('auth-modal').classList.remove('show');
      document.body.classList.remove('auth-locked');
    }

    function showDeleteInstanceModal(target) {
      deleteInstanceTarget = target || null;
      const modal = document.getElementById('delete-instance-modal');
      const sub = document.getElementById('delete-instance-sub');
      const warning = document.getElementById('delete-instance-warning');
      const err = document.getElementById('delete-instance-error');
      const pw = document.getElementById('delete-instance-password');
      const btn = document.getElementById('delete-instance-submit');
      const appLabel = String(target?.app || '').toUpperCase();
      const instanceLabel = String(target?.instanceName || `#${target?.instanceId || ''}`).trim();

      sub.textContent = `Enter your Web UI password to remove ${appLabel} instance "${instanceLabel}".`;
      if (target?.discardUnsaved) {
        warning.style.display = 'block';
        warning.textContent = 'You have unsaved configuration changes. Removing this instance will discard them.';
      } else {
        warning.style.display = 'none';
        warning.textContent = '';
      }
      err.textContent = '';
      pw.value = '';
      btn.disabled = false;
      modal.classList.add('show');
      setTimeout(() => pw.focus(), 50);
    }

    function hideDeleteInstanceModal() {
      document.getElementById('delete-instance-modal').classList.remove('show');
      document.getElementById('delete-instance-error').textContent = '';
      document.getElementById('delete-instance-password').value = '';
      document.getElementById('delete-instance-submit').disabled = false;
      deleteInstanceTarget = null;
    }

    async function submitDeleteInstance() {
      if (!deleteInstanceTarget) return;
      const msg = document.getElementById('settings-msg');
      const err = document.getElementById('delete-instance-error');
      const pw = document.getElementById('delete-instance-password');
      const btn = document.getElementById('delete-instance-submit');
      const confirmPassword = String(pw.value || '');
      err.textContent = '';
      btn.disabled = true;

      try {
        const r = await apiFetch('/api/instances/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            app: deleteInstanceTarget.app,
            instance_id: deleteInstanceTarget.instanceId,
            confirm_password: confirmPassword,
          }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          err.textContent = data.error || 'Remove failed';
          btn.disabled = false;
          return;
        }
        hideDeleteInstanceModal();
        window.settingsActiveTab = 'global';
        msg.textContent = 'Instance removed';
        settingsStatusMessage = 'Instance removed';
        syncSettingsSaveFab();
        await loadSettings();
        await refresh();
        showToast('Instance Removed', 'Returned to Global Settings.');
      } catch (e) {
        err.textContent = 'Remove failed';
        btn.disabled = false;
      }
    }

    async function ensureAuth() {
      const modal = document.getElementById('auth-modal');
      if (modal.classList.contains('show')) return;
      if (authInFlight) return;
      authInFlight = true;
      if (!authHeader) loadAuthHeader();
      if (authHeader) {
        const ok = await apiFetch('/api/status').then(r => r.ok).catch(() => false);
        if (ok) {
          hideAuthModal();
          await refresh();
          startTimers();
          authInFlight = false;
          return;
        }
        clearAuthHeader();
      }
      const st = await fetch('/api/auth/status', { cache: 'no-store' }).then(r => r.json()).catch(() => ({}));
      passwordIsSet = !!st.password_set;
      showAuthModal(passwordIsSet ? 'login' : 'set');
      authInFlight = false;
    }

    async function authSubmit() {
      const btn = document.getElementById('auth-submit');
      const err = document.getElementById('auth-error');
      const pw = String(document.getElementById('auth-password').value || '');
      err.textContent = '';
      btn.disabled = true;

      if (!passwordIsSet) {
        const r = await fetch('/api/auth/bootstrap', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: pw }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          err.textContent = data.error || 'Failed to set password';
          btn.disabled = false;
          return;
        }
        passwordIsSet = true;
      }

      authHeader = 'Basic ' + btoa('seekarr:' + pw);
      const testOk = await apiFetch('/api/status').then(r => r.ok).catch(() => false);
      if (!testOk) {
        err.textContent = 'Invalid password';
        clearAuthHeader();
        btn.disabled = false;
        return;
      }
      saveAuthHeader();

      hideAuthModal();
      await refresh();
      startTimers();
    }
