/**
 * FR-CR-05-136 — Calendar proxy for the meeting-pipeline
 * title-match step.
 *
 * Deploy as: Web App, "Execute as me", "Anyone with the link".
 * Set the SHARED_TOKEN script property before deploying.
 *
 * The Python pipeline calls this endpoint with:
 *   GET <web-app-url>?token=<SHARED_TOKEN>&from=<ISO>&to=<ISO>
 *
 * Returns JSON:
 *   { events: [
 *       { title, start, end, attendees: [{email, name}], description }
 *     ] }
 *
 * On bad/missing token returns:
 *   { error: "unauthorized" }
 */

function doGet(e) {
  const params = (e && e.parameter) || {};
  const expected = PropertiesService
    .getScriptProperties()
    .getProperty('SHARED_TOKEN');

  if (!expected || params.token !== expected) {
    return _json({ error: 'unauthorized' });
  }

  const fromIso = params.from;
  const toIso = params.to;
  if (!fromIso || !toIso) {
    return _json({ error: 'missing_from_or_to' });
  }

  const from = new Date(fromIso);
  const to = new Date(toIso);
  if (isNaN(from) || isNaN(to)) {
    return _json({ error: 'invalid_dates' });
  }

  const cal = CalendarApp.getDefaultCalendar();
  const events = cal.getEvents(from, to);

  const out = events.map(function(ev) {
    var attendees = [];
    try {
      attendees = ev.getGuestList(true).map(function(g) {
        return { email: g.getEmail() || '', name: g.getName() || '' };
      });
    } catch (err) {
      attendees = [];
    }
    return {
      title: ev.getTitle() || '',
      start: ev.getStartTime().toISOString(),
      end: ev.getEndTime().toISOString(),
      attendees: attendees,
      description: ev.getDescription() || '',
    };
  });

  return _json({ events: out });
}


function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}


/**
 * One-time helper: run from the Apps Script editor to set the
 * shared token. Replace 'CHANGE_ME' with the value you'll put
 * into Python's `CALENDAR_APPS_SCRIPT_SHARED_TOKEN` env var.
 */
function setSharedToken() {
  PropertiesService
    .getScriptProperties()
    .setProperty('SHARED_TOKEN', 'CHANGE_ME');
}
