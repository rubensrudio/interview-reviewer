import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';

import { ApiError } from '../../core/http/api-error';
import {
  ExpectedLevel,
  PlanProposal,
  RequirementClassification,
  RequirementItem,
  RequirementItemInput,
} from './session-api';

/**
 * Item of the list emitted by `changed`: existing items keep every received field, new or
 * split-off items are `RequirementItemInput` with `id: null`. Matches
 * `SessionApi.replaceRequirementList`.
 */
export type RequirementDraft = RequirementItem | RequirementItemInput;

interface Option<T extends string> {
  readonly value: T;
  readonly label: string;
}

const LEVELS: readonly ExpectedLevel[] = ['junior', 'mid-level', 'senior', 'expert'];

const LEVEL_OPTIONS: readonly Option<ExpectedLevel>[] = [
  { value: 'junior', label: 'Junior' },
  { value: 'mid-level', label: 'Mid-level' },
  { value: 'senior', label: 'Senior' },
  { value: 'expert', label: 'Expert' },
];

const CLASSIFICATION_OPTIONS: readonly Option<RequirementClassification>[] = [
  { value: 'required', label: 'Required' },
  { value: 'nice_to_have', label: 'Nice to have' },
];

const PENDING_MESSAGE = 'Some requirements need clarification before you continue.';

const ERROR_MESSAGES: Readonly<Record<string, string>> = {
  NO_REQUIRED_SKILLS: 'Define at least one required technical skill to continue.',
  TOO_MANY_REQUIRED_SKILLS:
    'This job lists {count} required skills. The limit is 20 — review the list and remove or merge {excess} before continuing.',
  PENDING_CLARIFICATION: PENDING_MESSAGE,
};

/** A merge done in this editor, kept so that "Undo merge" restores the exact source items. */
interface MergeRecord {
  readonly id: string;
  readonly terms: readonly string[];
  readonly sources: readonly RequirementDraft[];
}

function toInput(item: RequirementDraft): RequirementItemInput {
  return {
    id: null,
    name: item.name,
    original_terms: [...item.original_terms],
    classification: item.classification,
    level: item.level,
  };
}

function highestLevel(levels: readonly (ExpectedLevel | null)[]): ExpectedLevel | null {
  return levels.reduce<ExpectedLevel | null>((best, level) => {
    if (level === null) {
      return best;
    }
    return best === null || LEVELS.indexOf(level) > LEVELS.indexOf(best) ? level : best;
  }, null);
}

function isPending(item: RequirementDraft): boolean {
  return 'pending_clarification' in item && item.pending_clarification;
}

function sameTerms(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((term, i) => term === b[i]);
}

/**
 * Editable list of job requirements and plan proposal (CT-62).
 *
 * Stateless towards the server: every edit emits the whole new list through `changed`
 * (the consumer persists it with `replaceRequirementList`); merge and split happen here.
 * Count limits are enforced by the backend and only rendered from `error`.
 */
@Component({
  selector: 'app-requirement-list-editor',
  templateUrl: './requirement-list-editor.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      --text: #1a1a1a;
      --text-muted: #4b5563;
      --border: #c4c4c4;
      --border-soft: #e5e7eb;
      --surface: #ffffff;
      --surface-sunken: #f9fafb;
      --accent: #1d4ed8;
      --pending-bg: #fffbeb;
      --pending-border: #b45309;
      --pending-text: #78350f;
      --danger: #b91c1c;
      --danger-bg: #fef2f2;
      display: grid;
      gap: 1.5rem;
      color: var(--text);
    }
    h2,
    h3 {
      margin: 0;
      font-size: 1.125rem;
      font-weight: 600;
      letter-spacing: -0.01em;
    }
    .hint {
      margin: 0.25rem 0 0;
      color: var(--text-muted);
      font-size: 0.875rem;
    }
    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 0.5rem;
    }
    table {
      width: 100%;
      min-width: 44rem;
      border-collapse: collapse;
      font-size: 0.9375rem;
    }
    th,
    td {
      padding: 0.625rem 0.75rem;
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid var(--border-soft);
    }
    th {
      color: var(--text-muted);
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      background: var(--surface-sunken);
    }
    tbody tr:last-child td {
      border-bottom: 0;
    }
    tr.pending td {
      background: var(--pending-bg);
    }
    tr.pending td:first-child {
      box-shadow: inset 4px 0 0 var(--pending-border);
    }
    .terms {
      color: var(--text-muted);
    }
    .badge {
      display: inline-block;
      margin-top: 0.375rem;
      padding: 0.125rem 0.5rem;
      border: 1px solid var(--pending-border);
      border-radius: 999px;
      color: var(--pending-text);
      font-size: 0.75rem;
      font-weight: 600;
    }
    .question {
      margin: 0.25rem 0 0;
      color: var(--pending-text);
      font-size: 0.8125rem;
    }
    input[type='text'],
    select {
      width: 100%;
      min-height: 2.5rem;
      padding: 0.375rem 0.5rem;
      border: 1px solid var(--border);
      border-radius: 0.375rem;
      color: var(--text);
      background: var(--surface);
      font: inherit;
    }
    input[type='checkbox'] {
      width: 1.25rem;
      height: 1.25rem;
      margin: 0.625rem 0 0;
    }
    button {
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border: 1px solid var(--text-muted);
      border-radius: 0.375rem;
      color: var(--text);
      background: var(--surface);
      font: inherit;
      cursor: pointer;
      transition:
        background-color 120ms ease,
        border-color 120ms ease;
    }
    button:hover:not(:disabled) {
      background: var(--surface-sunken);
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }
    button.primary {
      border-color: var(--accent);
      color: #ffffff;
      background: var(--accent);
    }
    button.primary:hover:not(:disabled) {
      background: #1e40af;
    }
    button.link {
      min-height: 2.5rem;
      padding: 0.25rem 0.5rem;
      border-color: transparent;
      background: transparent;
    }
    button.remove {
      color: var(--danger);
    }
    input:focus-visible,
    select:focus-visible,
    button:focus-visible {
      outline: 3px solid var(--accent);
      outline-offset: 2px;
    }
    .row-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.25rem;
    }
    .toolbar,
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.75rem;
    }
    .actions {
      justify-content: flex-end;
    }
    .add {
      display: grid;
      grid-template-columns: minmax(10rem, 2fr) minmax(8rem, 1fr) minmax(8rem, 1fr) auto;
      align-items: end;
      gap: 0.75rem;
    }
    @media (max-width: 40rem) {
      .add {
        grid-template-columns: 1fr;
      }
    }
    .add label {
      display: grid;
      gap: 0.25rem;
      font-size: 0.875rem;
      color: var(--text-muted);
    }
    .empty {
      margin: 0;
      padding: 1.5rem;
      border: 1px dashed var(--border);
      border-radius: 0.5rem;
      color: var(--text-muted);
      text-align: center;
    }
    .error {
      margin: 0;
      padding: 0.75rem 1rem;
      border-left: 4px solid var(--danger);
      border-radius: 0.25rem;
      color: var(--danger);
      background: var(--danger-bg);
    }
    .notice {
      margin: 0;
      color: var(--pending-text);
      font-size: 0.875rem;
    }
    .non-technical,
    .proposal {
      display: grid;
      gap: 0.75rem;
      padding: 1rem 1.25rem;
      border: 1px solid var(--border);
      border-radius: 0.5rem;
    }
    .non-technical ul,
    .proposal ul {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .non-technical li,
    .proposal li {
      padding: 0.25rem 0.625rem;
      border: 1px solid var(--border-soft);
      border-radius: 999px;
      background: var(--surface-sunken);
      font-size: 0.875rem;
    }
    .label {
      display: inline-block;
      color: var(--text-muted);
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
    .count {
      font-size: 1.5rem;
      font-weight: 600;
      letter-spacing: -0.02em;
    }
  `,
})
export class RequirementListEditor {
  readonly items = input.required<RequirementItem[]>();
  readonly nonTechnical = input<string[]>([]);
  readonly proposal = input<PlanProposal | null>(null);
  readonly error = input<ApiError | null>(null);

  readonly changed = output<RequirementDraft[]>();
  readonly confirmList = output<void>();
  readonly confirmPlan = output<void>();

  protected readonly levelOptions = LEVEL_OPTIONS;
  protected readonly classificationOptions = CLASSIFICATION_OPTIONS;
  protected readonly pendingMessage = PENDING_MESSAGE;

  /** Indexes of the rows selected for merge; cleared whenever a new list arrives. */
  protected readonly selected = linkedSignal<RequirementItem[], ReadonlySet<number>>({
    source: this.items,
    computation: () => new Set<number>(),
  });

  /**
   * Set after emitting a confirmation; any new input from the consumer (list, proposal or error)
   * re-enables the buttons. Prevents double submission without an extra input.
   */
  private readonly confirmationSent = linkedSignal({
    source: () => [this.items(), this.proposal(), this.error()] as const,
    computation: () => false,
  });

  private readonly merges = signal<readonly MergeRecord[]>([]);

  protected readonly newName = signal('');
  protected readonly newClassification = signal<RequirementClassification>('required');
  protected readonly newLevel = signal<ExpectedLevel | null>(null);

  protected readonly hasPending = computed(() => this.items().some(isPending));
  protected readonly canMerge = computed(() => this.selected().size >= 2);
  protected readonly canAdd = computed(() => this.newName().trim().length > 0);
  protected readonly confirmListDisabled = computed(
    () => this.hasPending() || this.confirmationSent(),
  );
  protected readonly confirmPlanDisabled = computed(() => this.confirmationSent());

  protected readonly errorMessage = computed(() => {
    const error = this.error();
    if (!error) {
      return null;
    }
    const template = ERROR_MESSAGES[error.code];
    if (!template) {
      return error.message;
    }
    const details = error.details ?? {};
    return template.replace(/\{(\w+)\}/g, (match, key: string) => {
      const value = details[key];
      return typeof value === 'number' || typeof value === 'string' ? String(value) : match;
    });
  });

  protected readonly showPendingNotice = computed(
    () => this.hasPending() && this.errorMessage() !== PENDING_MESSAGE,
  );

  protected readonly questionLabel = computed(() => {
    const count = this.proposal()?.planned_count ?? 0;
    return `${count} ${count === 1 ? 'question' : 'questions'}`;
  });

  protected isPending(item: RequirementItem): boolean {
    return isPending(item);
  }

  protected canSplit(item: RequirementItem): boolean {
    return item.original_terms.length > 1;
  }

  protected rename(index: number, event: Event): void {
    const field = event.target as HTMLInputElement;
    const current = this.items()[index];
    const name = field.value.trim();
    if (!name) {
      field.value = current.name;
      return;
    }
    if (name === current.name) {
      return;
    }
    this.update(index, { name });
  }

  protected setClassification(index: number, event: Event): void {
    const value = (event.target as HTMLSelectElement).value as RequirementClassification;
    this.update(index, { classification: value });
  }

  protected setLevel(index: number, event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.update(index, { level: value ? (value as ExpectedLevel) : null });
  }

  protected remove(index: number): void {
    this.changed.emit(this.items().filter((_, i) => i !== index));
  }

  protected toggleSelected(index: number): void {
    const next = new Set(this.selected());
    if (next.has(index)) {
      next.delete(index);
    } else {
      next.add(index);
    }
    this.selected.set(next);
  }

  protected merge(): void {
    const items = this.items();
    const indexes = [...this.selected()].sort((a, b) => a - b);
    if (indexes.length < 2) {
      return;
    }
    const sources = indexes.map((i) => items[i]);
    const [target] = sources;
    const terms = [...new Set(sources.flatMap((s) => s.original_terms))];
    const merged: RequirementItem = {
      ...target,
      original_terms: terms,
      classification: sources.some((s) => s.classification === 'required')
        ? 'required'
        : 'nice_to_have',
      level: highestLevel(sources.map((s) => s.level)),
      pending_clarification: sources.some((s) => s.pending_clarification),
      clarification_question:
        sources.find((s) => s.clarification_question)?.clarification_question ?? null,
    };

    this.merges.update((records) => [
      ...records.filter((r) => r.id !== target.id),
      { id: target.id, terms, sources: [target, ...sources.slice(1).map(toInput)] },
    ]);
    this.selected.set(new Set());

    const rest = new Set(indexes.slice(1));
    this.changed.emit(
      items.flatMap((item, i) => (i === indexes[0] ? [merged] : rest.has(i) ? [] : [item])),
    );
  }

  /**
   * Undoes a merge: restores the source items of a merge done here, otherwise splits the item
   * into one item per original term (the first keeps the id).
   */
  protected split(index: number): void {
    const items = this.items();
    const item = items[index];
    const record = this.merges().find(
      (r) => r.id === item.id && sameTerms(r.terms, item.original_terms),
    );
    let parts: RequirementDraft[];
    if (record) {
      parts = [...record.sources];
      this.merges.update((records) => records.filter((r) => r !== record));
    } else {
      const [first, ...others] = item.original_terms;
      parts = [
        { ...item, original_terms: [first] },
        ...others.map((term): RequirementItemInput => ({
          id: null,
          name: term,
          original_terms: [term],
          classification: item.classification,
          level: item.level,
        })),
      ];
    }
    this.changed.emit(
      items.flatMap((current, i): RequirementDraft[] => (i === index ? parts : [current])),
    );
  }

  protected setNewName(event: Event): void {
    this.newName.set((event.target as HTMLInputElement).value);
  }

  protected setNewClassification(event: Event): void {
    this.newClassification.set(
      (event.target as HTMLSelectElement).value as RequirementClassification,
    );
  }

  protected setNewLevel(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.newLevel.set(value ? (value as ExpectedLevel) : null);
  }

  protected add(event: Event): void {
    event.preventDefault();
    const name = this.newName().trim();
    if (!name) {
      return;
    }
    const created: RequirementItemInput = {
      id: null,
      name,
      original_terms: [name],
      classification: this.newClassification(),
      level: this.newLevel(),
    };
    this.newName.set('');
    this.newClassification.set('required');
    this.newLevel.set(null);
    this.changed.emit([...this.items(), created]);
  }

  protected requestListConfirmation(): void {
    if (this.confirmListDisabled()) {
      return;
    }
    this.confirmationSent.set(true);
    this.confirmList.emit();
  }

  protected requestPlanConfirmation(): void {
    if (this.confirmPlanDisabled()) {
      return;
    }
    this.confirmationSent.set(true);
    this.confirmPlan.emit();
  }

  private update(index: number, patch: Partial<RequirementItem>): void {
    this.changed.emit(this.items().map((item, i) => (i === index ? { ...item, ...patch } : item)));
  }
}
