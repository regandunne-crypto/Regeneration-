/* ============================================================
   Draft decision logic — pure functions, no DOM.

   This is where the "my saved draft disappeared" bug lived, so it is kept
   separate from app.js and unit tested directly (tests/test_draft_logic.js).
   Loaded by index.html as a plain script before app.js; no build step.
   ============================================================ */

(function (root) {
  'use strict';

  /** Server rows use editing_test_id; browser copies use editingTestId. */
  function getDraftEditingId(draft) {
    if (!draft) return null;
    return draft.editing_test_id || draft.editingTestId || null;
  }

  function draftTimestamp(draft) {
    const parsed = Date.parse((draft && draft.updated_at) || '');
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function draftQuestions(draft) {
    return draft && Array.isArray(draft.questions) ? draft.questions : [];
  }

  /**
   * A draft of an untouched form is not worth offering to restore. Autosave
   * fires on any input, so empty drafts do get written.
   */
  function draftHasContent(draft) {
    if (!draft) return false;
    if ((draft.title || '').trim()) return true;
    if ((draft.chapter || '').trim() || (draft.description || '').trim()) return true;
    return draftQuestions(draft).some((q) => (
      (q && (q.q || '').trim())
      || (q && (q.explanation || '').trim())
      || (q && Array.isArray(q.options) && q.options.some((opt) => (opt || '').trim()))
    ));
  }

  /**
   * Pick between the server copy and the browser copy.
   *
   * The browser copy is what survives a Render redeploy wiping the ephemeral
   * store, so it must win when it is genuinely newer — but only by a margin, so
   * that a copy written moments before the server round-trip does not look
   * newer on every single load.
   */
  function chooseNewerDraft(serverDraft, localDraft, skewMs) {
    const skew = typeof skewMs === 'number' ? skewMs : 2000;
    const serverUsable = draftHasContent(serverDraft);
    const localUsable = draftHasContent(localDraft);
    if (!serverUsable && !localUsable) return { draft: null, source: null };
    if (serverUsable && !localUsable) return { draft: serverDraft, source: 'server' };
    if (!serverUsable && localUsable) return { draft: localDraft, source: 'browser' };
    return draftTimestamp(localDraft) > draftTimestamp(serverDraft) + skew
      ? { draft: localDraft, source: 'browser' }
      : { draft: serverDraft, source: 'server' };
  }

  /**
   * Decide what the editor should do when it opens.
   *
   * Returns one of:
   *   { action: 'blank' }       start an empty new test, draft left untouched
   *   { action: 'saved-test' }  load the saved test being edited
   *   { action: 'resume' }      restore the draft without asking
   *   { action: 'ask', kind }   ask the lecturer; kind is 'create-vs-draft'
   *                             or 'edit-vs-draft'
   *
   * A draft is never discarded here. Discarding only happens after the
   * lecturer explicitly chooses to.
   */
  function decideDraftAction(options) {
    const opts = options || {};
    const mode = opts.mode === 'edit' ? 'edit' : 'create';
    const editingTestId = opts.editingTestId || null;
    const draft = opts.draft || null;
    const draftEditingId = getDraftEditingId(draft);
    const editing = mode === 'edit' && !!editingTestId;

    if (!draftHasContent(draft)) {
      return { action: editing ? 'saved-test' : 'blank', draftEditingId: null };
    }
    if (opts.resumeDraft) {
      return { action: 'resume', draftEditingId };
    }
    if (editing) {
      // A draft of exactly this test: restore it, as before.
      if (draftEditingId === editingTestId) {
        return { action: 'resume', draftEditingId };
      }
      // A draft of something else: warn, because the first autosave of this
      // edit would otherwise replace it silently.
      return { action: 'ask', kind: 'edit-vs-draft', draftEditingId, belongsElsewhere: true };
    }
    // Create mode. A draft with no editing_test_id is a new-test draft — the
    // case the old code could never restore, because it only matched drafts
    // whose editing_test_id equalled the test being edited.
    return {
      action: 'ask',
      kind: 'create-vs-draft',
      draftEditingId,
      belongsElsewhere: !!draftEditingId
    };
  }

  const api = {
    getDraftEditingId,
    draftTimestamp,
    draftQuestions,
    draftHasContent,
    chooseNewerDraft,
    decideDraftAction
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this);
