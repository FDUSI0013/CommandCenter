/**
 * Feedback scores and governance events.
 *
 * Half of these assert field ceilings, which looks fussy until you see what
 * happens without them: the ingest path answers 200 and marks the individual
 * row `malformed`, so an over-long `action_taken` is a silent data loss the
 * caller has no way to notice. Clamping locally turns "the event vanished" into
 * "the event arrived with a truncated label".
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { FulcrumOps } from '../src/index.js';
import { withStub } from './helpers/stub-server.js';

const TRACE_ID = '11111111-1111-4111-8111-111111111111';

describe('feedback scores', () => {
  it('posts a score against a trace', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.score({ id: TRACE_ID, name: 'helpfulness', value: 0.8, reason: 'resolved on first reply' });
      await client.close();

      const score = stub.itemsFor('scores')[0]!;
      assert.equal(score.id, TRACE_ID);
      assert.equal(score.name, 'helpfulness');
      assert.equal(score.value, 0.8);
      assert.equal(score.target, 'trace');
      assert.equal(score.source, 'sdk');
      assert.equal(score.reason, 'resolved on first reply');
    });
  });

  it('targets a span or a thread when asked to', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.scores([
        { id: TRACE_ID, name: 'a', value: 1, target: 'span' },
        { id: 'conversation-9', name: 'b', value: 2, target: 'thread' },
      ]);
      await client.close();

      assert.deepEqual(
        stub.itemsFor('scores').map((score) => score.target),
        ['span', 'thread'],
      );
    });
  });

  it('folds a score into the item it scores while that item is still open', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace('scored-inline', (trace) => {
        trace.score({ name: 'confidence', value: 0.95, categoryName: 'model' });
      });
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      const scores = trace.feedback_scores as Record<string, unknown>[];
      assert.equal(scores.length, 1);
      assert.equal(scores[0]!.name, 'confidence');
      assert.equal(
        stub.itemsFor('scores').length,
        0,
        'no second request: the score travelled with the trace',
      );
    });
  });

  it('scoreCurrent() attaches to whatever is in scope', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      await client.trace('outer', async () => {
        await client.span('inner', async () => {
          client.scoreCurrent({ name: 'step-ok', value: 1 });
        });
      });
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      const span = (trace.spans as Record<string, unknown>[])[0]!;
      assert.equal((span.feedback_scores as Record<string, unknown>[])[0]!.name, 'step-ok');
    });
  });

  it('refuses a score with no id, name or numeric value', async () => {
    await withStub(async (stub, baseUrl) => {
      const errors: string[] = [];
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: (error) => errors.push(error.message),
      });
      client.score({ id: '', name: 'x', value: 1 });
      client.score({ id: TRACE_ID, name: '', value: 1 });
      client.score({ id: TRACE_ID, name: 'x', value: Number.NaN });
      await client.close();

      assert.equal(stub.itemsFor('scores').length, 0);
      assert.equal(errors.length, 3, 'each refusal was reported, none thrown');
    });
  });
});

describe('governance events', () => {
  it('reports a guardrail firing, correlated to the run in scope', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
      });
      let traceId = '';
      await client.trace('answer', async (trace) => {
        traceId = trace.id;
        client.guardrailTriggered({
          guardrail: 'PII masking',
          actionTaken: 'Mask',
          score: 0.91,
          matched: { entity: 'email', count: 1 },
          ref: 'idempotency-1',
        });
      });
      await client.close();

      const event = stub.itemsFor('events')[0]!;
      assert.equal(event.kind, 'guardrail.triggered');
      assert.equal(event.guardrail, 'PII masking');
      assert.equal(event.action_taken, 'Mask');
      assert.equal(event.score, 0.91);
      assert.equal(event.trace_id, traceId, 'the event found the open trace by itself');
      assert.equal(event.ref, 'idempotency-1');
      assert.deepEqual(event.matched, { entity: 'email', count: 1 });
    });
  });

  // A span opened with nothing in scope gets an implicit trace to belong to,
  // and that trace never enters the context. Reading the trace id off the span
  // is what stops the event arriving with a span id whose run is unknown.
  it('correlates an event raised inside a span that opened its own trace', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = await FulcrumOps.create({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
      });
      let spanId = '';
      let spanTraceId = '';
      await client.span({ name: 'classify', type: 'guardrail' }, async (span) => {
        spanId = span.id;
        spanTraceId = span.traceId;
        client.guardrailTriggered({ guardrail: 'PII masking', actionTaken: 'Mask' });
      });
      await client.close();

      const event = stub.itemsFor('events')[0]!;
      assert.equal(event.span_id, spanId);
      assert.equal(event.trace_id, spanTraceId, 'the event was attributed to the implicit trace');
      assert.equal(stub.itemsFor('traces')[0]!.id, spanTraceId);
    });
  });

  it('reports a policy violation', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.policyViolation({
        policy: 'no-financial-advice',
        severity: 'High',
        actionTaken: 'Blocked',
        detail: { rule: 'advice-detector', confidence: 0.77 },
      });
      await client.close();

      const event = stub.itemsFor('events')[0]!;
      assert.equal(event.kind, 'policy.violation');
      assert.equal(event.policy, 'no-financial-advice');
      assert.equal(event.severity, 'High');
      assert.deepEqual(event.detail, { rule: 'advice-detector', confidence: 0.77 });
    });
  });

  it('reports end-user feedback', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.submitFeedback({
        rating: 5,
        sentiment: 'positive',
        body: 'Exactly what I needed.',
        source: 'in-app',
        submittedBy: 'user-42',
        traceId: TRACE_ID,
      });
      await client.close();

      const event = stub.itemsFor('events')[0]!;
      assert.equal(event.kind, 'feedback.submitted');
      assert.equal(event.rating, 5);
      assert.equal(event.sentiment, 'positive');
      assert.equal(event.body, 'Exactly what I needed.');
      assert.equal(event.submitted_by, 'user-42');
      assert.equal(event.trace_id, TRACE_ID);
    });
  });

  it('refuses an event that names no guardrail or policy', async () => {
    await withStub(async (stub, baseUrl) => {
      const errors: string[] = [];
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: (error) => errors.push(error.message),
      });
      client.guardrailTriggered({ guardrail: '   ' });
      client.policyViolation({ policy: '' });
      await client.close();

      assert.equal(stub.itemsFor('events').length, 0);
      assert.equal(errors.length, 2);
    });
  });
});

describe('contract field limits', () => {
  it('trims the event fields the API measures in tens of characters', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.policyViolation({
        policy: 'p'.repeat(300),
        // 24 and 16 respectively — easy ceilings to overshoot with a sentence.
        actionTaken: 'Blocked and escalated to the on-call reviewer',
        severity: 'Extremely severe indeed',
      });
      client.submitFeedback({
        sentiment: 'cautiously optimistic',
        source: 's'.repeat(80),
        submittedBy: 'u'.repeat(400),
      });
      await client.close();

      const [violation, feedback] = stub.itemsFor('events');
      assert.equal(String(violation!.policy).length, 160);
      assert.equal(String(violation!.action_taken).length, 24);
      assert.equal(String(violation!.severity).length, 16);
      assert.equal(String(feedback!.sentiment).length, 16);
      assert.equal(String(feedback!.source).length, 40);
      assert.equal(String(feedback!.submitted_by).length, 160);
    });
  });

  it('clamps a rating into the 1–5 the contract accepts', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.submitFeedback({ rating: 0 });
      client.submitFeedback({ rating: 9 });
      client.submitFeedback({ rating: 3.6 });
      await client.close();

      assert.deepEqual(
        stub.itemsFor('events').map((event) => event.rating),
        [1, 5, 4],
      );
    });
  });

  it('trims names, tags and metadata to what the contract allows', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace(
        {
          name: 'n'.repeat(500),
          tags: [...Array.from({ length: 60 }, (_v, index) => `tag-${index}`), 'duplicate', 'duplicate'],
          threadId: 't'.repeat(300),
        },
        (trace) => {
          const span = trace.startSpan({ name: 'child', model: 'm'.repeat(400), provider: 'p'.repeat(200) });
          span.end({});
        },
      );
      await client.close();

      const trace = stub.itemsFor('traces')[0]!;
      assert.equal(String(trace.name).length, 200);
      assert.equal((trace.tags as string[]).length, 32);
      assert.equal(new Set(trace.tags as string[]).size, 32, 'de-duplicated as well as capped');
      assert.equal(String(trace.thread_id).length, 120);

      const span = (trace.spans as Record<string, unknown>[])[0]!;
      assert.equal(String(span.model).length, 120);
      assert.equal(String(span.provider).length, 80);
    });
  });

  it('keeps at most 25 scores on one item, as the contract requires', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace('many-scores', (trace) => {
        for (let index = 0; index < 40; index += 1) trace.score({ name: `score-${index}`, value: index });
      });
      await client.close();

      assert.equal((stub.itemsFor('traces')[0]!.feedback_scores as unknown[]).length, 25);
    });
  });

  it('rounds usage to the integers the contract declares', async () => {
    await withStub(async (stub, baseUrl) => {
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace('usage', (trace) => {
        trace.startSpan({ name: 'llm', type: 'llm', usage: { prompt_tokens: 10.4, completion_tokens: 5.6 } }).end({});
      });
      await client.close();

      const span = (stub.itemsFor('traces')[0]!.spans as Record<string, unknown>[])[0]!;
      assert.deepEqual(span.usage, { prompt_tokens: 10, completion_tokens: 6, total_tokens: 16 });
    });
  });
});

describe('per-item ingest results', () => {
  it('reports a row the ingest path rejected, without interrupting anyone', async () => {
    await withStub(async (stub, baseUrl) => {
      const errors: Array<{ code: string; operation: string }> = [];
      stub.inject('POST', '/api/v1/ingest/traces', {
        status: 200,
        body: {
          received: 1,
          accepted: 0,
          rejected: 1,
          blocked: 0,
          results: [{ index: 0, outcome: 'rejected', code: 'unknown_agent', reason: 'No agent named "ghost".' }],
        },
      });
      const client = new FulcrumOps({
        apiKey: 'k',
        baseUrl,
        bootstrap: false,
        setAsDefault: false,
        onError: (error, context) => errors.push({ code: error.code, operation: context.operation }),
      });
      const result = await client.trace('ghosted', async () => 'the body still ran');
      assert.equal(result, 'the body still ran');
      await client.close();

      assert.deepEqual(errors, [{ code: 'unknown_agent', operation: 'ingest:traces' }]);
      assert.equal(client.getStats().rejected, 1);
    });
  });

  it('remembers the quota state the ingest path reported', async () => {
    await withStub(async (stub, baseUrl) => {
      stub.inject('POST', '/api/v1/ingest/traces', {
        status: 200,
        body: {
          received: 1,
          accepted: 1,
          rejected: 0,
          blocked: 0,
          quotas: [
            {
              id: 'q-1',
              name: 'Traces per month',
              resource: 'traces',
              scope: 'workspace',
              unit: 'count',
              limit_value: 1000,
              used_value: 940,
              remaining: 60,
              utilization_pct: 94,
              enforcement: 'soft',
              status: 'Warning',
            },
          ],
        },
      });
      const client = new FulcrumOps({ apiKey: 'k', baseUrl, bootstrap: false, setAsDefault: false });
      client.trace('quota-watch', () => undefined);
      await client.close();

      const quotas = client.getQuotas();
      assert.equal(quotas.length, 1);
      assert.equal(quotas[0]!.status, 'Warning');
      assert.equal(quotas[0]!.remaining, 60);
    });
  });
});
