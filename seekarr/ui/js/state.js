    let authHeader = '';
    let passwordIsSet = false;
    let authInFlight = false;
    let authMode = '';
    let timersStarted = false;
    let timezoneOptionsLoaded = false;
    let activeTimeZone = '';
    let activeDateFormat = 'iso';
    let activeClockFormat = '24h';
    let refreshTimer = null;
    let countdownTimer = null;
    let statusData = null;
    let settingsBaseline = '';
    let settingsDirty = false;
    let settingsStatusMessage = '';
    let deleteInstanceTarget = null;
    let toastSeq = 0;
    const recentItemMetaCache = new Map();
    const authStorageKey = 'seekarr_auth_header';
    const timezoneFallback = [
      'UTC', 'Etc/UTC',
      'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Phoenix',
      'America/Anchorage', 'Pacific/Honolulu',
      'Europe/London', 'Europe/Paris', 'Europe/Berlin',
      'Asia/Tokyo', 'Asia/Seoul', 'Asia/Kolkata', 'Asia/Singapore', 'Asia/Shanghai',
      'Australia/Sydney', 'Australia/Perth'
    ];
