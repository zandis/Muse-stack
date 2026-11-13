import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { en, type Dict, type Key } from './en';
import { HTML_LANG, LANGS, pickLang, type Lang } from './format';
import { zhTW } from './zh-TW';
import { readStored, writeStored } from '../lib/storage';

export type { Key } from './en';
export type { Lang } from './format';

export const DICTS: Record<Lang, Dict> = { 'zh-TW': zhTW, en };
export const LANG_STORAGE_KEY = 'muse.lang';

/** Fill {0} {1} … in a template. */
export function fill(template: string, args: readonly (string | number)[]): string {
  return template.replace(/\{(\d+)\}/g, (whole, n: string) => {
    const v = args[Number(n)];
    return v === undefined ? whole : String(v);
  });
}

export function translate(lang: Lang, key: Key, ...args: (string | number)[]): string {
  return fill(DICTS[lang][key], args);
}

export type T = (key: Key, ...args: (string | number)[]) => string;

/** null means "same as this device". */
export type LangChoice = Lang | null;

interface I18n {
  lang: Lang;
  choice: LangChoice;
  setChoice: (choice: LangChoice) => void;
  t: T;
}

const Ctx = createContext<I18n | null>(null);

export function systemLang(): Lang {
  const nav = typeof navigator === 'undefined' ? undefined : navigator;
  return pickLang(nav?.languages?.length ? nav.languages : nav?.language ? [nav.language] : []);
}

function storedChoice(): LangChoice {
  const v = readStored(LANG_STORAGE_KEY);
  return (LANGS as readonly string[]).includes(v ?? '') ? (v as Lang) : null;
}

export function I18nProvider({ children, initial }: { children: ReactNode; initial?: Lang }) {
  const [choice, setChoiceState] = useState<LangChoice>(() => initial ?? storedChoice());
  const lang = choice ?? systemLang();
  const setChoice = useCallback((next: LangChoice) => {
    writeStored(LANG_STORAGE_KEY, next);
    setChoiceState(next);
  }, []);
  useEffect(() => {
    document.documentElement.lang = HTML_LANG[lang];
  }, [lang]);
  const value = useMemo<I18n>(() => ({ lang, choice, setChoice, t: (key, ...args) => translate(lang, key, ...args) }), [lang, choice, setChoice]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18n {
  const v = useContext(Ctx);
  if (!v) throw new Error('useI18n needs an I18nProvider');
  return v;
}
