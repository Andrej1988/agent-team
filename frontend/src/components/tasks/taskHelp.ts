import type { Task, TaskStage } from '../../api/client'

export interface TaskHelp {
  title: string
  problem: string
  input: string
  next: string
  target: 'plan' | 'answer' | 'stage' | 'login' | 'run' | null
  stageSeq?: number
  context?: string
  details?: string
}

function short(text: string | null | undefined, max = 240): string {
  const line = (text ?? '').replace(/\s+/g, ' ').trim()
  return line.length > max ? `${line.slice(0, max - 1)}…` : line
}

// Use concrete recorded evidence. Unknown causes keep their original detail;
// we never invent a fix or infer a credential failure from a generic error.
function evidence(stage?: TaskStage): string | undefined {
  const finding = stage?.findings?.find((f) => !f.resolved)
  if (finding) {
    if (/checked_at|confirmation time|confirmation timestamp/i.test(finding.text) && /fail|fehlgeschlagen/i.test(finding.text)) {
      return `${finding.id}: Nach einem fehlgeschlagenen Abruf wird der alte Prüfzeitpunkt fälschlich aktualisiert. Dieser Fehler muss noch korrigiert werden.`
    }
    return `${finding.id}: ${short(finding.text)}`
  }
  return short(stage?.summary) || undefined
}

export function errorHelp(error: string, pipeline = false): TaskHelp {
  const lower = error.toLowerCase()
  const base: TaskHelp = {
    title: 'Aktion fehlgeschlagen', problem: 'Die Aktion konnte nicht abgeschlossen werden.',
    input: 'Keine konkrete Rückfrage des Agenten.',
    next: 'Technische Meldung unten prüfen. Wenn die Ursache unklar ist, diese Meldung zur Fehlerprüfung weitergeben.',
    target: null, details: error,
  }
  if (/claude-anmeldung|oauth.*expir|failed to authenticate|invalid authentication credentials/.test(lower)) {
    return { ...base, title: 'Claude-Anmeldung erforderlich', problem: 'Die Claude-Anmeldung ist abgelaufen oder ungültig.',
      input: 'Erneut bei Claude anmelden.', next: '„Claude erneut anmelden“ öffnen, die Browser-Anmeldung abschließen und danach den Lauf wiederholen.', target: 'login' }
  }
  if (/permission denied|root:root|root-owned|cannot unlink|sudo.*(unavailable|absent)/.test(lower)) {
    return { ...base, title: 'Dateirechte blockieren den Agenten', problem: 'Der Agent darf eine benötigte Datei oder ein Verzeichnis nicht ändern.',
      input: 'Die betroffenen Dateirechte müssen von jemandem mit ausreichenden Rechten korrigiert werden.',
      next: pipeline ? 'Dateien aus der technischen Meldung gezielt prüfen lassen. Danach „Resume“ und „Run next“ verwenden; Limits ändern hilft hier nicht.' : 'Dateirechte korrigieren lassen und anschließend den Lauf erneut starten.', target: pipeline ? 'plan' : 'run' }
  }
  if (/(credentials|api.?key|api.?secret).*(missing|absent|not set|not configured)|fehlende.*(zugang|credential)/.test(lower)) {
    return { ...base, title: 'Zugangsdaten fehlen', problem: 'Dem Agenten fehlen Zugangsdaten für einen benötigten Dienst.',
      input: 'Die in der Meldung genannten Zugangsdaten in der passenden Projekt-.env hinterlegen.',
      next: 'Zugangsdaten lokal hinterlegen und dem Task mitteilen, dass sie verfügbar sind. Keine Schlüssel im Task-Text oder Chat einfügen.', target: null }
  }
  if (/empty plan|no parseable/.test(lower)) {
    return { ...base, title: 'Plan konnte nicht erstellt werden', problem: 'Der Planungsagent hat keinen verwendbaren Plan geliefert.',
      input: 'Keine fachliche Antwort erforderlich.', next: '„Re-plan“ starten. Wenn der Fehler wiederkehrt, das Plan-Log prüfen lassen.', target: 'plan' }
  }
  if (/429|rate.?limit|usage.?limit|spend.?limit|quota|capacity|limit reached|usage cap/.test(lower)) {
    return { ...base, title: 'KI-Anbieter begrenzt den Lauf', problem: 'Der KI-Anbieter lässt derzeit keine weiteren Anfragen zu.',
      input: 'Auf verfügbare Kapazität warten oder bewusst ein Modell eines anderen verfügbaren Anbieters wählen.',
      next: pipeline ? 'Nach Freigabe die fehlgeschlagene Stufe mit „Retry“ wiederholen; bei einem Plan-Lauf „Re-plan“ verwenden.' : 'Nach Freigabe den Lauf erneut starten. Zeitpunkt und Anbieter stehen in den technischen Details.', target: pipeline ? 'stage' : 'run' }
  }
  if (/failed to fetch|networkerror|network error|502|503|504|worker.*(not|unreachable|nicht)|timeout|timed out/.test(lower)) {
    return { ...base, title: 'Dienst nicht erreichbar', problem: 'Dashboard, Worker oder externer Dienst antwortet nicht rechtzeitig.',
      input: 'Keine fachliche Antwort erforderlich.', next: 'Verbindung und Dienst prüfen, dann erneut versuchen. Wenn es wiederholt passiert, die technische Meldung prüfen lassen.' }
  }
  if (/409|conflict/.test(lower)) {
    return { ...base, title: 'Aktion im aktuellen Zustand nicht möglich', problem: 'Der Task hat sich inzwischen geändert oder erfüllt die Voraussetzungen dieser Aktion nicht.',
      input: 'Aktuellen Task-Status prüfen.', next: 'Task neu laden und die für diesen Zustand angebotene Aktion verwenden.' }
  }
  if (/422|validation|invalid|must be|cannot be/.test(lower)) {
    return { ...base, title: 'Eingabe wurde abgelehnt', problem: 'Eine Eingabe oder Aktion erfüllt die erforderlichen Bedingungen nicht.',
      input: 'Die in der technischen Meldung genannte Eingabe korrigieren.', next: 'Eingabe prüfen und anschließend erneut speichern oder ausführen.' }
  }
  return base
}

export function taskHelp(task: Task, stages: TaskStage[]): TaskHelp | null {
  const pipeline = task.mode === 'pipeline'
  const ordered = stages.filter((s) => s.task_id === task.id).sort((a, b) => a.seq - b.seq)
  const last = [...ordered].reverse().find((s) => s.status === 'done' || s.status === 'failed')
  const waiting = ordered.find((s) => s.status === 'awaiting_input')
  const failed = [...ordered].reverse().find((s) => s.status === 'failed')
  const reason = task.stop_reason || task.error || ''
  const plan = task.plan
  const detail = [reason, task.status === 'blocked' ? plan?.rationale : null].filter(Boolean).join('\n\n')
  const base: TaskHelp = {
    title: 'Task benötigt Aufmerksamkeit', problem: short(reason) || 'Der Task wurde angehalten.',
    input: 'Keine konkrete Rückfrage des Agenten.', next: 'Technische Details prüfen lassen, bevor der Task fortgesetzt wird.',
    target: null, context: evidence(last), details: detail,
  }
  if (waiting && (task.status === 'awaiting_input' || task.status === 'plan_ready')) {
    const questions = waiting.questions ?? []
    return { ...base, title: `Antwort für Stufe ${waiting.seq} benötigt`,
      problem: questions.length ? short(waiting.summary) || 'Der Agent kann ohne deine Antwort nicht weiterarbeiten.' : 'Der Agent verlangt eine Antwort, hat aber keine konkrete Frage angegeben.',
      input: questions.length ? 'Die Fragen im Antwortfeld unten beantworten.' : 'Den Agenten bitten, die fehlende Information konkret zu benennen.',
      next: questions.length ? 'Antwort eingeben und „Answer“ klicken. Nur diese Stufe wird mit deiner Antwort fortgesetzt.' : 'Im Antwortfeld nachfragen: „Welche konkrete Information brauchst du von mir?“',
      target: 'answer', stageSeq: waiting.seq, context: undefined, details: waiting.summary || undefined }
  }
  if (task.status === 'limit_reached') {
    const help = errorHelp(task.error || 'usage limit reached', pipeline)
    return { ...help, target: pipeline ? failed ? 'stage' : 'plan' : 'run', stageSeq: failed?.seq, details: task.error || undefined }
  }
  if (task.status === 'failed') {
    const help = errorHelp(task.error || failed?.error || 'Der Agentenlauf wurde abgebrochen.', pipeline)
    if (help.title === 'Aktion fehlgeschlagen') {
      help.title = failed ? `Stufe ${failed.seq} ist fehlgeschlagen` : 'Agentenlauf fehlgeschlagen'
      help.problem = 'Der Agentenlauf wurde abgebrochen; ein verwendbares Ergebnis liegt nicht vor.'
      help.next = pipeline
        ? failed ? `Ursache in den technischen Details prüfen. Nach der Korrektur Stufe ${failed.seq} mit „Retry“ wiederholen.` : 'Ursache in den technischen Details prüfen und anschließend „Re-plan“ starten.'
        : 'Ursache in den technischen Details prüfen und anschließend den Lauf erneut starten.'
      help.target = failed ? 'stage' : pipeline ? 'plan' : 'run'
    }
    return { ...help, stageSeq: failed?.seq, context: evidence(failed), details: task.error || failed?.error || undefined }
  }
  if (task.status === 'blocked') {
    if (/stopped by (user|human)/i.test(reason)) return { ...base, title: 'Pipeline manuell gestoppt',
      problem: 'Die Pipeline wurde per „Stop“ angehalten.', input: 'Entscheiden, ob die Arbeit fortgesetzt werden soll.',
      next: 'Zum Fortsetzen „Resume“ klicken, dann „Run next“.', target: 'plan', context: undefined }
    if (/budget exceeded/i.test(reason)) return { ...base, title: 'Kostenbudget erreicht',
      problem: `Die erfassten Kosten überschreiten das Budget${task.budget_usd == null ? '' : ` von $${task.budget_usd.toFixed(2)}`}.`,
      input: 'Entscheiden, ob du weitere Kosten freigeben möchtest.',
      next: 'Bei Zustimmung „budget USD“ erhöhen und „Save plan“ klicken. Danach „Resume“ und „Run next“. Sonst den Task angehalten lassen.', target: 'plan' }
    if (/max_stages exceeded/i.test(reason)) return { ...base, title: 'Stufenlimit reicht nicht aus',
      problem: `Der vorgeschlagene Plan benötigt mehr als die erlaubten ${plan?.max_stages ?? '?'} Stufen.`,
      input: 'Entscheiden, ob mehr Stufen erlaubt werden sollen.',
      next: 'Bei Zustimmung „max stages“ erhöhen und „Save plan“ klicken, dann „Re-plan“. „Resume“ allein ergänzt den abgelehnten Plan nicht.', target: 'plan' }
    if (/max_rounds exceeded/i.test(reason) || (/orchestrator proposed no further stages/i.test(reason) && plan && (task.round_count ?? 0) >= plan.max_rounds)) {
      return { ...base, title: 'Korrekturlimit erreicht',
        problem: `${task.round_count ?? 0} von ${plan?.max_rounds ?? '?'} erlaubten Fehlerrunden sind verbraucht. Noch offene Fehler verhindern den Abschluss.`,
        input: 'Entscheiden, ob weitere Korrekturversuche erlaubt werden sollen.',
        next: 'Bei Zustimmung „max rounds“ erhöhen und „Save plan“ klicken, dann „Re-plan“ für Korrektur, Tests und Abschlussprüfung. „Resume“ allein behebt den Fehler nicht.', target: 'plan' }
    }
    if (/same unresolved finding.*twice/i.test(reason)) return { ...base, title: 'Derselbe Fehler besteht weiterhin',
      problem: 'Die Prüfung hat denselben offenen Fehler zweimal gemeldet; die automatische Korrektur wurde angehalten.',
      input: 'Keine fachliche Antwort erforderlich. Die bisherige Korrektur muss geprüft werden.',
      next: 'Den aktuellen Befund prüfen lassen und einen gezielten Korrekturschritt in den Plan aufnehmen. Danach „Re-plan“ oder den geänderten Plan speichern und fortsetzen.', target: 'plan' }
    const known = errorHelp(reason, true)
    if (known.title !== 'Aktion fehlgeschlagen') return { ...base, ...known, context: evidence(last), details: detail }
    if (/final completed stage is not a reviewer/i.test(reason)) return { ...base, title: 'Abschlussprüfung fehlt im Plan',
      problem: 'Die letzte abgeschlossene Stufe war keine Abschlussprüfung. Der Planungsagent hat trotzdem keine weiteren Stufen vorgesehen.',
      input: 'Keine fachliche Antwort erforderlich. Der Plan muss um die fehlende Prüfung ergänzt werden.',
      next: '„Re-plan“ starten und eine abschließende Reviewer-Stufe verlangen. Bei offenen Testfehlern muss davor eine Korrektur mit erneuten Tests eingeplant werden.', target: 'plan' }
    if (/backtest-auditor/i.test(reason)) return { ...base, title: 'Backtest-Prüfung fehlt oder ist fehlgeschlagen',
      problem: 'Die erforderliche unabhängige Prüfung des letzten Backtests ist nicht erfolgreich abgeschlossen.',
      input: 'Keine fachliche Antwort erforderlich. Prüfungen bzw. Korrekturen fehlen im Plan.',
      next: '„Re-plan“ starten, damit Backtest-Prüfung und gegebenenfalls Korrekturen vor dem abschließenden Reviewer ergänzt werden.', target: 'plan' }
    if (/reviewer verdict|exit criteria unmet|orchestrator proposed no further stages/i.test(reason)) return { ...base, title: 'Task noch nicht erfolgreich geprüft',
      problem: 'Die Abschlussbedingungen sind nicht erfüllt. Der Planungsagent hat keinen weiteren Arbeitsschritt geliefert.',
      input: 'Keine konkrete Rückfrage. Offene Befunde müssen bearbeitet und erneut geprüft werden.',
      next: 'Den aktuellen Befund prüfen lassen und „Re-plan“ für die fehlende Korrektur bzw. Abschlussprüfung starten.', target: 'plan' }
    return { ...base, title: 'Agent meldet ein Hindernis',
      problem: short(last?.summary) || short(reason) || 'Der Agent konnte nicht weiterarbeiten und hat keine Ursache angegeben.',
      input: last?.questions?.length ? 'Die vom Agenten genannten Fragen klären.' : 'Der Agent hat keine konkrete Rückfrage angegeben. Die Ursache muss anhand seiner Meldung geklärt werden.',
      next: 'Hindernis aus der Meldung beheben oder konkretisieren lassen. Danach „Resume“ und „Run next“, falls noch Stufen vorhanden sind; sonst „Re-plan“.', target: 'plan' }
  }
  if (task.status === 'awaiting_input') return { ...base, title: 'Rückfrage noch nicht verfügbar',
    problem: 'Der Task wartet auf eine Antwort. Die zugehörige Stufe ist noch nicht geladen.',
    input: 'Auf die konkrete Frage warten.', next: 'Task neu laden, wenn das Antwortfeld nicht erscheint.' }
  if (task.status === 'plan_ready' && pipeline) {
    const pending = ordered.find((s) => s.status === 'pending')
    if (failed) return { ...base, title: 'Fehlgeschlagene Stufe muss behandelt werden',
      problem: `Stufe ${failed.seq} ist fehlgeschlagen. Daher kann die nächste Stufe noch nicht starten.`,
      input: 'Ursache prüfen und entscheiden, ob die Stufe wiederholt werden soll.',
      next: `Nach Behebung Stufe ${failed.seq} mit „Retry“ wiederholen.`, target: 'stage', stageSeq: failed.seq, details: failed.error || undefined }
    return { ...base, title: 'Pipeline wartet auf deinen Start',
      problem: pending ? `Stufe ${pending.seq} (${pending.role}) ist geplant, aber noch nicht gestartet.` : 'Es sind keine weiteren Stufen geplant; der Abschluss muss noch geprüft werden.',
      input: 'Den nächsten Arbeitsschritt bewusst starten.', next: pending ? '„Run next“ klicken, um die nächste Stufe zu starten.' : '„Finish“ klicken. Fehlt eine erfolgreiche Abschlussprüfung, wird neu geplant.',
      target: 'plan', context: undefined, details: undefined }
  }
  return null
}
