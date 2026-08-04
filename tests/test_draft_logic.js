/* Unit tests for the draft decision logic (draft_utils.js).
   Run directly:  node --test tests/
   Also run as part of `pytest` via tests/test_draft_logic.py. */

const test = require('node:test');
const assert = require('node:assert');
const {
  getDraftEditingId,
  draftHasContent,
  chooseNewerDraft,
  decideDraftAction
} = require('../draft_utils.js');

const at = (iso) => new Date(iso).toISOString();

function draft(overrides = {}) {
  return Object.assign({
    title: 'Chapter 3 Forces',
    chapter: '',
    description: '',
    questions: [{ q: 'What is a newton?', options: ['a', 'b', 'c', 'd'], correct: 0, explanation: '' }],
    editing_test_id: null,
    updated_at: at('2026-08-01T10:00:00Z')
  }, overrides);
}

test('getDraftEditingId reads both server and browser field names', () => {
  assert.strictEqual(getDraftEditingId({ editing_test_id: 'abc' }), 'abc');
  assert.strictEqual(getDraftEditingId({ editingTestId: 'xyz' }), 'xyz');
  assert.strictEqual(getDraftEditingId({}), null);
  assert.strictEqual(getDraftEditingId(null), null);
});

test('draftHasContent ignores an untouched form', () => {
  assert.strictEqual(draftHasContent(null), false);
  assert.strictEqual(draftHasContent({ title: '  ', questions: [] }), false);
  assert.strictEqual(
    draftHasContent({ title: '', questions: [{ q: '', options: ['', '', '', ''], explanation: '' }] }),
    false
  );
  assert.strictEqual(draftHasContent({ title: 'Something', questions: [] }), true);
  assert.strictEqual(
    draftHasContent({ title: '', questions: [{ q: '', options: ['', 'Newton', '', ''], explanation: '' }] }),
    true
  );
  assert.strictEqual(draftHasContent({ title: '', chapter: 'Chapter 2', questions: [] }), true);
});

test('chooseNewerDraft prefers the browser copy only when clearly newer', () => {
  const server = draft({ updated_at: at('2026-08-01T10:00:00Z') });
  const older = draft({ title: 'Browser copy', updated_at: at('2026-08-01T09:00:00Z') });
  const newer = draft({ title: 'Browser copy', updated_at: at('2026-08-01T11:00:00Z') });

  assert.strictEqual(chooseNewerDraft(server, older).source, 'server');
  assert.strictEqual(chooseNewerDraft(server, newer).source, 'browser');

  // Within the clock-skew margin the server copy still wins.
  const barelyNewer = draft({ updated_at: at('2026-08-01T10:00:01Z') });
  assert.strictEqual(chooseNewerDraft(server, barelyNewer).source, 'server');
});

test('chooseNewerDraft falls back to whichever copy exists', () => {
  const server = draft();
  assert.strictEqual(chooseNewerDraft(server, null).source, 'server');
  assert.strictEqual(chooseNewerDraft(null, server).source, 'browser');
  assert.deepStrictEqual(chooseNewerDraft(null, null), { draft: null, source: null });
  // An empty draft counts as no draft.
  assert.strictEqual(chooseNewerDraft({ title: '', questions: [] }, null).source, null);
});

// ── The regression the whole fix exists for ─────────────────────────────────

test('REGRESSION: a create-mode draft is offered when Create New Test is opened', () => {
  // editing_test_id is null because the draft was made while building a brand
  // new test. The old client only restored drafts whose editing_test_id equalled
  // the test being edited, so this draft could never be recovered — and create
  // mode deleted it outright before it was ever read.
  const decision = decideDraftAction({ mode: 'create', editingTestId: null, draft: draft() });
  assert.strictEqual(decision.action, 'ask');
  assert.strictEqual(decision.kind, 'create-vs-draft');
  assert.strictEqual(decision.belongsElsewhere, false);
});

test('create mode with no draft starts blank without asking', () => {
  assert.strictEqual(decideDraftAction({ mode: 'create', draft: null }).action, 'blank');
  assert.strictEqual(
    decideDraftAction({ mode: 'create', draft: { title: '', questions: [] } }).action,
    'blank'
  );
});

test('create mode warns when the draft belongs to a saved test', () => {
  const decision = decideDraftAction({
    mode: 'create',
    draft: draft({ editing_test_id: 'test-77' })
  });
  assert.strictEqual(decision.action, 'ask');
  assert.strictEqual(decision.kind, 'create-vs-draft');
  assert.strictEqual(decision.belongsElsewhere, true);
  assert.strictEqual(decision.draftEditingId, 'test-77');
});

test('edit mode restores a draft of the same test without asking', () => {
  const decision = decideDraftAction({
    mode: 'edit',
    editingTestId: 'test-42',
    draft: draft({ editing_test_id: 'test-42' })
  });
  assert.strictEqual(decision.action, 'resume');
});

test('edit mode warns when the draft belongs to a different test', () => {
  const decision = decideDraftAction({
    mode: 'edit',
    editingTestId: 'test-42',
    draft: draft({ editing_test_id: 'test-77' })
  });
  assert.strictEqual(decision.action, 'ask');
  assert.strictEqual(decision.kind, 'edit-vs-draft');
});

test('edit mode warns when a new-test draft would be overwritten', () => {
  const decision = decideDraftAction({
    mode: 'edit',
    editingTestId: 'test-42',
    draft: draft({ editing_test_id: null })
  });
  assert.strictEqual(decision.action, 'ask');
  assert.strictEqual(decision.kind, 'edit-vs-draft');
});

test('edit mode with no draft loads the saved test', () => {
  const decision = decideDraftAction({ mode: 'edit', editingTestId: 'test-42', draft: null });
  assert.strictEqual(decision.action, 'saved-test');
});

test('an explicit Resume click skips the question', () => {
  const decision = decideDraftAction({ mode: 'create', draft: draft(), resumeDraft: true });
  assert.strictEqual(decision.action, 'resume');
});

test('no decision path ever discards a draft on its own', () => {
  const cases = [
    { mode: 'create', draft: draft() },
    { mode: 'create', draft: draft({ editing_test_id: 'test-1' }) },
    { mode: 'edit', editingTestId: 'test-1', draft: draft({ editing_test_id: 'test-2' }) },
    { mode: 'edit', editingTestId: 'test-1', draft: draft({ editing_test_id: 'test-1' }) }
  ];
  for (const input of cases) {
    const decision = decideDraftAction(input);
    assert.ok(
      decision.action === 'ask' || decision.action === 'resume',
      `a draft with content must never be dropped silently (got ${decision.action})`
    );
  }
});
