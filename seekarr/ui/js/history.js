    function renderHistorySection(data, instances) {
      const runsWrap = document.getElementById('runs-wrap');
      const sh = data.search_history || {};
      if (!window.historyPage) window.historyPage = {};

      const PAGE_SIZE = 24;
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
        const totalRows = rows.length;
        const totalPages = Math.ceil(totalRows / PAGE_SIZE) || 1;
        let currentPage = window.historyPage[key] || 1;
        if (currentPage > totalPages) currentPage = totalPages;
        window.historyPage[key] = currentPage;

        const startIdx = (currentPage - 1) * PAGE_SIZE;
        const pageRows = rows.slice(startIdx, startIdx + PAGE_SIZE);

        let body = '';
        for (const row of pageRows) {
          const appType = String(row.app_type || activeInst.app || '').trim().toLowerCase();
          const instanceId = Number(row.instance_id || activeInst.instance_id || 0);
          const kindMeta = actionKindMeta(row.action_kind, row.item_key);
          const itemOpenArgs = `${JSON.stringify(appType)}, ${JSON.stringify(String(instanceId))}, ${JSON.stringify(String(row.item_key || ''))}`;
          const rowClass = row.item_key ? 'history-entry' : 'history-entry no-cover';
          const dateLabel = fmtTime(row.occurred_at, { includeTime: false }) || '-';
          const timeLabel = fmtTime(row.occurred_at, { includeSeconds: false, omitDate: true }) || '';
          body += `
            <article class="history-list-item">
              <div class="history-entry-stamp mono" title="${safe(fmtTime(row.occurred_at) || '')}">
                <div class="history-time-date">${safe(dateLabel)}</div>
                <div class="history-time-clock">${safe(timeLabel)}</div>
              </div>
              <div class="${rowClass}" data-app="${safe(appType)}" data-instance-id="${safe(String(instanceId))}" data-item-key="${safe(String(row.item_key || ''))}">
                <div class="history-entry-cover-wrap${row.item_key ? '' : ' is-empty'}"></div>
                <div class="history-entry-main">
                  <button class="history-entry-title history-entry-link" type="button" onclick='openRecentActionItem(${itemOpenArgs}); return false;'>${safe(row.title || 'Untitled search')}</button>
                  <div class="history-entry-meta">${renderActionMetaBadges(kindMeta)}</div>
                </div>
              </div>
            </article>`;
        }
        if (!body) {
          body = `<div class="mono history-empty">No searches recorded yet.</div>`;
        }

        let paginationHtml = '';
        if (totalPages > 1) {
          paginationHtml = `<div class="history-pagination">
            <div class="history-pagination-info">Showing ${startIdx + 1} to ${Math.min(startIdx + PAGE_SIZE, totalRows)} of ${totalRows} entries</div>
            <div class="history-pagination-controls">
              <button class="btn-mini btn-mini-neutral" ${currentPage === 1 ? 'disabled' : ''} onclick='window.changeHistoryPage(${JSON.stringify(key)}, -1); return false;' type="button">Previous</button>
              <div class="page-status">Page ${currentPage} of ${totalPages}</div>
              <button class="btn-mini btn-mini-neutral" ${currentPage === totalPages ? 'disabled' : ''} onclick='window.changeHistoryPage(${JSON.stringify(key)}, 1); return false;' type="button">Next</button>
            </div>
          </div>`;
        }

        contentHtml = `
          <div class="card history-card">
            <div class="history-list-wrap">
              <div class="history-list-note">Times shown in ${safe(getTimeZoneLabel())}</div>
              <div class="history-list">${body}</div>
            </div>
            ${paginationHtml}
          </div>`;
      } else {
        contentHtml = `
          <div class="card history-card">
            <div class="mono history-empty">No searches recorded yet.</div>
          </div>`;
        tabsHtml = '';
      }

      runsWrap.innerHTML = tabsHtml + contentHtml;
      hydrateActionMediaRows(runsWrap);
    }

    window.setHistoryTab = function setHistoryTab(key) {
      if (!window.historyPage) window.historyPage = {};
      window.historyActiveTab = key;
      window.historyPage[key] = 1;
      if (!statusData) return;
      const instances = Array.isArray(statusData.config?.instances) ? statusData.config.instances : [];
      renderHistorySection(statusData, instances);
    };

    window.changeHistoryPage = function changeHistoryPage(key, delta) {
      if (!window.historyPage) window.historyPage = {};
      window.historyActiveTab = key;
      const current = Number(window.historyPage[key] || 1);
      window.historyPage[key] = Math.max(1, current + Number(delta || 0));
      if (!statusData) return;
      const instances = Array.isArray(statusData.config?.instances) ? statusData.config.instances : [];
      renderHistorySection(statusData, instances);
    };
