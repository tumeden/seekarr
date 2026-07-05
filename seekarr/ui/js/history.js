    function historyRowHtml(row, activeInst) {
      const appType = String(row.app_type || activeInst.app || '').trim().toLowerCase();
      const instanceId = Number(row.instance_id || activeInst.instance_id || 0);
      const kindMeta = actionKindMeta(row.action_kind, row.item_key);
      const itemUrl = String(row.item_url || '').trim();
      const coverUrl = String(row.cover_url || '').trim();
      const itemOpenArgs = `${JSON.stringify(appType)}, ${JSON.stringify(String(instanceId))}, ${JSON.stringify(String(row.item_key || ''))}, ${JSON.stringify(itemUrl)}`;
      const rowClass = row.item_key ? 'history-entry' : 'history-entry no-cover';
      const dateLabel = fmtTime(row.occurred_at, { includeTime: false }) || '-';
      const timeLabel = fmtTime(row.occurred_at, { includeSeconds: false, omitDate: true }) || '';
      const coverWrapClass = (row.item_key && coverUrl) ? '' : ' is-empty';
      const coverHtml = coverUrl
        ? `<img class="history-entry-cover" src="${safe(coverUrl)}" alt="" loading="eager" fetchpriority="high" decoding="async">`
        : '';
      const buttonAttrs = itemUrl ? ` data-item-url="${safe(itemUrl)}"` : '';
      return `
        <article class="history-list-item">
          <div class="history-entry-stamp mono" title="${safe(fmtTime(row.occurred_at) || '')}">
            <div class="history-time-date">${safe(dateLabel)}</div>
            <div class="history-time-clock">${safe(timeLabel)}</div>
          </div>
          <div class="${rowClass}" data-app="${safe(appType)}" data-instance-id="${safe(String(instanceId))}" data-item-key="${safe(String(row.item_key || ''))}" data-cover-url="${safe(coverUrl)}">
            <div class="history-entry-cover-wrap${coverWrapClass}">${coverHtml}</div>
            <div class="history-entry-main">
              <button class="history-entry-title history-entry-link" type="button"${buttonAttrs} onclick='openRecentActionItem(${itemOpenArgs}); return false;'>${safe(row.title || 'Untitled search')}</button>
              <div class="history-entry-meta">${renderActionMetaBadges(kindMeta)}</div>
            </div>
          </div>
        </article>`;
    }

    function appendHistoryRows(root, rows, activeInst, start, end) {
      const list = root.querySelector('.history-list');
      if (!list) return;
      const html = rows.slice(start, end).map(row => historyRowHtml(row, activeInst)).join('');
      if (html) list.insertAdjacentHTML('beforeend', html);
      settleActionCoverPaint(list);
      hydrateActionMediaRows(list);
    }

    function updateHistoryLoadMore(root, shown, total) {
      const status = root.querySelector('.history-load-status');
      if (status) status.textContent = total ? `Showing ${Math.min(shown, total)} of ${total} entries` : 'No searches recorded yet.';
    }

    function attachHistoryInfiniteLoader(root, key, rows, activeInst) {
      const sentinel = root.querySelector('.history-scroll-sentinel');
      const loadNext = () => {
        if (window.historyLoadingMore) return;
        window.historyLoadingMore = true;
        if (!window.historyVisibleRows) window.historyVisibleRows = {};
        try {
          let current = Number(window.historyVisibleRows[key] || 48);
          if (current >= rows.length) return;
          let loaded = 0;
          while (current < rows.length && loaded < 120) {
            const next = Math.min(rows.length, current + 48);
            appendHistoryRows(root, rows, activeInst, current, next);
            loaded += next - current;
            current = next;
            window.historyVisibleRows[key] = current;
            updateHistoryLoadMore(root, current, rows.length);
            const rect = sentinel?.getBoundingClientRect();
            if (!rect || rect.top > window.innerHeight + 2600) break;
          }
        } finally {
          window.historyLoadingMore = false;
        }
      };
      const ensureBuffer = () => {
        const rect = sentinel?.getBoundingClientRect();
        if (rect && rect.top < window.innerHeight + 2600) loadNext();
      };
      if (!sentinel || typeof IntersectionObserver === 'undefined') return;
      if (window.historyIntersectionObserver) {
        window.historyIntersectionObserver.disconnect();
      }
      if (window.historyScrollBufferHandler) {
        window.removeEventListener('scroll', window.historyScrollBufferHandler);
        window.removeEventListener('resize', window.historyScrollBufferHandler);
      }
      window.historyScrollBufferHandler = () => {
        if (window.historyScrollBufferFrame) return;
        window.historyScrollBufferFrame = requestAnimationFrame(() => {
          window.historyScrollBufferFrame = null;
          ensureBuffer();
        });
      };
      window.addEventListener('scroll', window.historyScrollBufferHandler, { passive: true });
      window.addEventListener('resize', window.historyScrollBufferHandler, { passive: true });
      window.historyIntersectionObserver = new IntersectionObserver((entries) => {
        if (entries.some(entry => entry.isIntersecting)) loadNext();
      }, { root: null, rootMargin: '2600px 0px', threshold: 0 });
      window.historyIntersectionObserver.observe(sentinel);
      ensureBuffer();
    }

    function renderHistorySection(data, instances, force=false) {
      const runsWrap = document.getElementById('runs-wrap');
      const sh = data.search_history || {};
      if (!window.historyVisibleRows) window.historyVisibleRows = {};

      const BATCH_SIZE = 48;
      const historyInstances = instances.filter(inst => {
        const key = `${inst.app}:${inst.instance_id}`;
        return Array.isArray(sh[key]) && sh[key].length > 0;
      });

      if (!window.historyActiveTab && historyInstances.length > 0) {
        window.historyActiveTab = `${historyInstances[0].app}:${historyInstances[0].instance_id}`;
      }
      if (
        window.historyActiveTab &&
        !historyInstances.some(inst => `${inst.app}:${inst.instance_id}` === window.historyActiveTab)
      ) {
        window.historyActiveTab = historyInstances.length
          ? `${historyInstances[0].app}:${historyInstances[0].instance_id}`
          : '';
      }

      let tabsHtml = '<div class="history-tabs">';
      historyInstances.forEach(inst => {
        const key = `${inst.app}:${inst.instance_id}`;
        const isActive = (window.historyActiveTab === key);
        tabsHtml += `<button class="tab-btn history-tab ${isActive ? 'active' : ''}" onclick='window.setHistoryTab(${JSON.stringify(key)}); return false;' type="button">${safe(inst.app).toUpperCase()} - ${safe(inst.instance_name)}</button>`;
      });
      tabsHtml += '</div>';

      let contentHtml = '';
      const activeInst = historyInstances.find(inst => `${inst.app}:${inst.instance_id}` === window.historyActiveTab);
      if (activeInst) {
        const key = window.historyActiveTab;
        const rows = sh[key] || [];
        const visibleRows = Math.min(rows.length, Math.max(BATCH_SIZE, Number(window.historyVisibleRows[key] || BATCH_SIZE)));
        window.historyVisibleRows[key] = visibleRows;
        const renderKey = JSON.stringify({
          tabs: historyInstances.map(inst => `${inst.app}:${inst.instance_id}:${inst.instance_name || ''}`),
          active: key,
          visibleRows,
          total: rows.length,
          rows: rows.slice(0, visibleRows).map(row => [
            row.id || '',
            row.item_key || '',
            row.item_url || '',
            row.cover_url || '',
            row.title || '',
            row.occurred_at || '',
          ]),
        });
        if (!force && runsWrap.innerHTML && renderKey === lastHistoryRenderKey) {
          attachHistoryInfiniteLoader(runsWrap, key, rows, activeInst);
          return;
        }
        lastHistoryRenderKey = renderKey;

        const body = rows.length
          ? rows.slice(0, visibleRows).map(row => historyRowHtml(row, activeInst)).join('')
          : `<div class="mono history-empty">No searches recorded yet.</div>`;

        contentHtml = `
          <div class="card history-card">
            <div class="history-list-wrap">
              <div class="history-list-note">Times shown in ${safe(getTimeZoneLabel())}</div>
              <div class="history-list">${body}</div>
            </div>
            <div class="history-load-panel">
              <div class="history-load-status">Showing ${Math.min(visibleRows, rows.length)} of ${rows.length} entries</div>
            </div>
            <div class="history-scroll-sentinel" aria-hidden="true"></div>
          </div>`;
      } else {
        const renderKey = JSON.stringify({ tabs: [], active: '', visibleRows: 0, total: 0, rows: [] });
        if (!force && runsWrap.innerHTML && renderKey === lastHistoryRenderKey) return;
        lastHistoryRenderKey = renderKey;
        contentHtml = `
          <div class="card history-card">
            <div class="mono history-empty">No searches recorded yet.</div>
          </div>`;
        tabsHtml = '';
      }

      runsWrap.innerHTML = tabsHtml + contentHtml;
      settleActionCoverPaint(runsWrap);
      hydrateActionMediaRows(runsWrap);
      if (activeInst) {
        attachHistoryInfiniteLoader(runsWrap, window.historyActiveTab, sh[window.historyActiveTab] || [], activeInst);
      }
    }

    window.setHistoryTab = function setHistoryTab(key) {
      if (window.historyIntersectionObserver) {
        window.historyIntersectionObserver.disconnect();
        window.historyIntersectionObserver = null;
      }
      if (window.historyScrollBufferHandler) {
        window.removeEventListener('scroll', window.historyScrollBufferHandler);
        window.removeEventListener('resize', window.historyScrollBufferHandler);
        window.historyScrollBufferHandler = null;
      }
      if (!window.historyVisibleRows) window.historyVisibleRows = {};
      window.historyActiveTab = key;
      window.historyVisibleRows[key] = 48;
      if (!statusData) return;
      const instances = Array.isArray(statusData.config?.instances) ? statusData.config.instances : [];
      renderHistorySection(statusData, instances, true);
    };
