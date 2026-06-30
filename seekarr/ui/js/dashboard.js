    function fmtCountdown(iso) {
      if (!iso) return '';
      const t = Date.parse(iso);
      if (!Number.isFinite(t)) return '';
      const diff = Math.floor((t - Date.now()) / 1000);
      if (diff <= 0) return 'DUE';
      const h = Math.floor(diff / 3600);
      const m = Math.floor((diff % 3600) / 60);
      const s = diff % 60;
      if (h > 0) return `${h}h ${m}m`;
      if (m > 0) return `${m}m ${s}s`;
      return `${s}s`;
    }

    function tickCountdowns() {
      document.querySelectorAll('[data-next-sync]').forEach(el => {
        const iso = el.getAttribute('data-next-sync');
        const cd = fmtCountdown(iso);
        el.textContent = cd;
        if (el.classList.contains('big-countdown')) {
          el.classList.toggle('due', cd === 'DUE');
        }
      });
    }



    async function forceRunInstance(app, instanceId) {
      const msg = document.getElementById('msg');
      msg.textContent = `Force run started for ${app}:${instanceId}...`;
      const r = await apiFetch('/api/run_instance', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ app, instance_id: instanceId, force: true })
      });
      const data = await r.json();
      if (!r.ok) {
        msg.textContent = data.error || 'Failed to start run';
        return;
      }

      msg.textContent = (data.message || 'Run started') + ' (waiting for completion...)';

      // Poll briefly so the UI gives immediate feedback even when 0 actions are triggered.
      const key = `${app}:${instanceId}`;
      const startedMs = Date.now();
      for (let i = 0; i < 40; i++) {
        await new Promise(res => setTimeout(res, 500));
        let st;
        try {
          st = await (await apiFetch('/api/status', { cache:'no-store' })).json();
        } catch (e) {
          continue;
        }
        const rs = st.run_state || {};
        const lr = st.instance_last_run ? st.instance_last_run[key] : null;
        const finished = !rs.running;
        if (!finished) continue;
        if (lr && lr.finished_at) {
          const fin = Date.parse(lr.finished_at);
          if (Number.isFinite(fin) && fin >= (startedMs - 2000)) {
            const s = lr.stats || {};
            msg.textContent =
              `${app.toUpperCase()} ${lr.instance_name || ''} finished: ` +
              `wanted ${s.wanted_count ?? '-'}, ` +
              `triggered ${s.actions_triggered ?? '-'}, ` +
              `cooldown ${s.actions_skipped_cooldown ?? '-'}, ` +
              `not-released ${s.actions_skipped_not_released ?? '-'}, ` +
              `rate ${s.actions_skipped_rate_limit ?? '-'}.`;
            break;
          }
        }
      }

      await refresh();
    }

