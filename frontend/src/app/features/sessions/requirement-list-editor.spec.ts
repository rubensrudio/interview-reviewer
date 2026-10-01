import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ApiError } from '../../core/http/api-error';
import { RequirementDraft, RequirementListEditor } from './requirement-list-editor';
import { PlanProposal, RequirementItem } from './session-api';

function item(
  overrides: Partial<RequirementItem> & Pick<RequirementItem, 'id' | 'name'>,
): RequirementItem {
  return {
    original_terms: [overrides.name],
    classification: 'required',
    level: null,
    pending_clarification: false,
    clarification_question: null,
    ...overrides,
  };
}

const PYTHON = item({ id: 'r1', name: 'Python', level: 'senior' });
const DOCKER = item({ id: 'r2', name: 'Docker', classification: 'nice_to_have' });
const K8S = item({ id: 'r3', name: 'Kubernetes', original_terms: ['Kubernetes', 'K8s'] });

describe('RequirementListEditor', () => {
  let fixture: ComponentFixture<RequirementListEditor>;
  let root: HTMLElement;
  let changes: RequirementDraft[][];
  let listConfirmations: number;
  let planConfirmations: number;

  function render(inputs: {
    items?: RequirementItem[];
    nonTechnical?: string[];
    proposal?: PlanProposal | null;
    error?: ApiError | null;
  }): void {
    fixture.componentRef.setInput('items', inputs.items ?? [PYTHON, DOCKER]);
    fixture.componentRef.setInput('nonTechnical', inputs.nonTechnical ?? []);
    fixture.componentRef.setInput('proposal', inputs.proposal ?? null);
    fixture.componentRef.setInput('error', inputs.error ?? null);
    fixture.detectChanges();
  }

  const text = (): string => (root.textContent ?? '').replace(/\s+/g, ' ');
  const button = (name: string): HTMLButtonElement => {
    const found = Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => (b.getAttribute('aria-label') ?? b.textContent ?? '').trim() === name,
    );
    if (!found) {
      throw new Error(`Button "${name}" not found`);
    }
    return found;
  };
  const control = <T extends HTMLElement>(label: string): T => {
    const found = root.querySelector<T>(`[aria-label="${label}"]`);
    if (!found) {
      throw new Error(`Control "${label}" not found`);
    }
    return found;
  };
  const choose = (label: string, value: string): void => {
    const select = control<HTMLSelectElement>(label);
    select.value = value;
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
  };
  const lastChange = (): RequirementDraft[] => changes[changes.length - 1];

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [RequirementListEditor] }).compileComponents();
    fixture = TestBed.createComponent(RequirementListEditor);
    root = fixture.nativeElement as HTMLElement;
    changes = [];
    listConfirmations = 0;
    planConfirmations = 0;
    fixture.componentInstance.changed.subscribe((items) => changes.push(items));
    fixture.componentInstance.confirmList.subscribe(() => listConfirmations++);
    fixture.componentInstance.confirmPlan.subscribe(() => planConfirmations++);
  });

  it('renders name, original terms, classification and level of each item', () => {
    render({ items: [PYTHON, K8S] });

    expect(control<HTMLInputElement>('Name of Python').value).toBe('Python');
    expect(text()).toContain('Kubernetes, K8s');
    expect(control<HTMLSelectElement>('Classification of Python').value).toBe('required');
    expect(control<HTMLSelectElement>('Expected level of Python').value).toBe('senior');
    expect(control<HTMLSelectElement>('Expected level of Kubernetes').value).toBe('');
  });

  it('emits changed with the updated item when the classification changes', () => {
    render({});

    choose('Classification of Python', 'nice_to_have');

    expect(changes.length).toBe(1);
    expect(lastChange()).toEqual([{ ...PYTHON, classification: 'nice_to_have' }, DOCKER]);
  });

  it('emits changed with the updated item when the level changes or is cleared', () => {
    render({});

    choose('Expected level of Docker', 'expert');
    expect(lastChange()[1]).toEqual({ ...DOCKER, level: 'expert' });

    choose('Expected level of Python', '');
    expect(lastChange()[0]).toEqual({ ...PYTHON, level: null });
  });

  it('emits a renamed item and ignores a blank name', () => {
    render({});
    const name = control<HTMLInputElement>('Name of Python');

    name.value = '  Python 3  ';
    name.dispatchEvent(new Event('change'));
    expect(lastChange()[0]).toEqual({ ...PYTHON, name: 'Python 3' });

    name.value = '   ';
    name.dispatchEvent(new Event('change'));
    expect(changes.length).toBe(1);
    expect(name.value).toBe('Python');
  });

  it('removes an item', () => {
    render({});

    button('Remove Python').click();

    expect(lastChange()).toEqual([DOCKER]);
  });

  it('adds a new item with a null id only when the name is filled', () => {
    render({});
    const add = button('Add skill');
    expect(add.disabled).toBe(true);

    const name = control<HTMLInputElement>('New skill name');
    name.value = ' Go ';
    name.dispatchEvent(new Event('input'));
    choose('New skill classification', 'nice_to_have');
    choose('New skill expected level', 'mid-level');
    expect(add.disabled).toBe(false);

    add.click();
    fixture.detectChanges();

    expect(lastChange()).toEqual([
      PYTHON,
      DOCKER,
      {
        id: null,
        name: 'Go',
        original_terms: ['Go'],
        classification: 'nice_to_have',
        level: 'mid-level',
      },
    ]);
    expect(name.value).toBe('');
  });

  it('merges the selected items into one item that keeps all original terms', () => {
    render({ items: [PYTHON, DOCKER, K8S] });
    const merge = button('Merge selected');
    expect(merge.disabled).toBe(true);

    control<HTMLInputElement>('Select Docker to merge').click();
    control<HTMLInputElement>('Select Kubernetes to merge').click();
    fixture.detectChanges();
    expect(merge.disabled).toBe(false);

    merge.click();

    expect(lastChange()).toEqual([
      PYTHON,
      { ...DOCKER, original_terms: ['Docker', 'Kubernetes', 'K8s'], classification: 'required' },
    ]);
  });

  it('undoes a merge done in the editor, restoring the original items', () => {
    render({ items: [PYTHON, DOCKER, K8S] });
    control<HTMLInputElement>('Select Python to merge').click();
    control<HTMLInputElement>('Select Docker to merge').click();
    fixture.detectChanges();
    button('Merge selected').click();

    // The consumer feeds the merged list back.
    render({ items: lastChange() as RequirementItem[] });
    button('Undo merge of Python').click();

    expect(lastChange()).toEqual([
      PYTHON,
      {
        id: null,
        name: 'Docker',
        original_terms: ['Docker'],
        classification: 'nice_to_have',
        level: null,
      },
      K8S,
    ]);
  });

  it('splits an item with several original terms into one item per term', () => {
    render({ items: [PYTHON, K8S] });
    expect(() => button('Undo merge of Python')).toThrow();

    button('Undo merge of Kubernetes').click();

    expect(lastChange()).toEqual([
      PYTHON,
      { ...K8S, original_terms: ['Kubernetes'] },
      { id: null, name: 'K8s', original_terms: ['K8s'], classification: 'required', level: null },
    ]);
  });

  it('marks pending_clarification items with a textual "Needs clarification" indication', () => {
    const pending = item({
      id: 'r9',
      name: 'Cloud',
      pending_clarification: true,
      clarification_question: 'Is cloud experience required or a plus?',
    });
    render({ items: [PYTHON, pending] });

    const rows = Array.from(root.querySelectorAll('tbody tr'));
    expect(rows[0].textContent).not.toContain('Needs clarification');
    expect(rows[1].textContent).toContain('Needs clarification');
    expect(rows[1].textContent).toContain('Is cloud experience required or a plus?');
  });

  it('blocks confirming the list while an item needs clarification', () => {
    render({ items: [item({ id: 'r9', name: 'Cloud', pending_clarification: true })] });

    const confirm = button('Confirm list');
    expect(confirm.disabled).toBe(true);
    expect(text()).toContain('Some requirements need clarification before you continue.');
  });

  it('emits confirmList once until the inputs change', () => {
    render({});

    button('Confirm list').click();
    fixture.detectChanges();
    button('Confirm list').click();
    expect(listConfirmations).toBe(1);

    render({ error: { code: 'NO_REQUIRED_SKILLS', message: 'x' } });
    button('Confirm list').click();
    expect(listConfirmations).toBe(2);
  });

  it('lists non-technical requirements as not evaluated', () => {
    render({ nonTechnical: ['English (fluent)', '5+ years of experience'] });

    const section = root.querySelector('[data-testid="non-technical"]');
    expect(section?.textContent).toContain('Not evaluated in this session');
    expect(section?.textContent).toContain('English (fluent)');
    expect(section?.textContent).toContain('5+ years of experience');
  });

  it('shows the TOO_MANY_REQUIRED_SKILLS message with count and excess', () => {
    render({
      error: {
        code: 'TOO_MANY_REQUIRED_SKILLS',
        message: 'Too many',
        details: { count: 23, excess: 3 },
      },
    });

    const alert = root.querySelector('[role="alert"]');
    expect(alert?.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'This job lists 23 required skills. The limit is 20 — review the list and remove or merge 3 before continuing.',
    );
  });

  it('shows the NO_REQUIRED_SKILLS message and falls back to the API message', () => {
    render({ error: { code: 'NO_REQUIRED_SKILLS', message: 'x' } });
    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      'Define at least one required technical skill to continue.',
    );

    render({ error: { code: 'LLM_UNAVAILABLE', message: 'Service unavailable.' } });
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('Service unavailable.');
  });

  it('does not block confirmation by counting required skills on the client', () => {
    const many = Array.from({ length: 25 }, (_, i) => item({ id: `r${i}`, name: `Skill ${i}` }));
    render({ items: many });

    expect(button('Confirm list').disabled).toBe(false);
  });

  it('shows the plan proposal with N questions, the skills and a confirm plan button', () => {
    render({ proposal: { planned_count: 4, skills: ['Python', 'Docker', 'SQL', 'AWS'] } });

    const proposal = root.querySelector('[data-testid="plan-proposal"]');
    expect(proposal?.textContent).toContain('4 questions');
    for (const skill of ['Python', 'Docker', 'SQL', 'AWS']) {
      expect(proposal?.textContent).toContain(skill);
    }

    button('Confirm plan').click();
    fixture.detectChanges();
    button('Confirm plan').click();
    expect(planConfirmations).toBe(1);
  });

  it('uses the singular for a single-question plan and hides the plan without proposal', () => {
    render({});
    expect(root.querySelector('[data-testid="plan-proposal"]')).toBeNull();

    render({ proposal: { planned_count: 1, skills: ['Python'] } });
    expect(root.querySelector('[data-testid="plan-proposal"]')?.textContent).toContain(
      '1 question',
    );
    expect(root.querySelector('[data-testid="plan-proposal"]')?.textContent).not.toContain(
      '1 questions',
    );
  });

  it('shows an empty state when there are no items', () => {
    render({ items: [] });

    expect(text()).toContain('No technical skills in the list yet.');
  });
});
