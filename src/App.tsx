import { useState, type FormEvent } from 'react'
import { parseDecklist, type ParsedLine } from './lib/parseDecklist'
import './App.css'

const SAMPLE_DECKLIST = `4 Lightning Bolt
1 Sol Ring
// ignore
Sideboard
2 Negate`

function App() {
  const [decklist, setDecklist] = useState(SAMPLE_DECKLIST)
  const [lines, setLines] = useState<ParsedLine[] | null>(null)

  function handleAnalyze(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setLines(parseDecklist(decklist))
  }

  const validLines = lines?.filter((line) => line.ok) ?? []
  const invalidLines = lines?.filter((line) => !line.ok) ?? []

  return (
    <main className="page">
      <header className="intro">
        <h1>Analisador de decks</h1>
        <p>
          Cole uma lista no formato <code>4 Lightning Bolt</code>. Nesta etapa
          só interpretamos quantidade e nome — ainda sem consultar o Scryfall.
        </p>
      </header>

      <form className="deck-form" onSubmit={handleAnalyze}>
        <label htmlFor="decklist">Decklist</label>
        <textarea
          id="decklist"
          name="decklist"
          rows={12}
          value={decklist}
          onChange={(event) => setDecklist(event.target.value)}
          spellCheck={false}
        />
        <button type="submit">Analisar</button>
      </form>

      {lines !== null && (
        <section className="results" aria-live="polite">
          <h2>Cartas reconhecidas ({validLines.length})</h2>
          {validLines.length === 0 ? (
            <p>Nenhuma linha válida nesta lista.</p>
          ) : (
            <ul>
              {validLines.map((line, index) => (
                <li key={`${line.name}-${index}`}>
                  <span className="qty">{line.quantity}</span> {line.name}
                </li>
              ))}
            </ul>
          )}

          {invalidLines.length > 0 && (
            <>
              <h2>Linhas inválidas ({invalidLines.length})</h2>
              <ul className="invalid">
                {invalidLines.map((line, index) => (
                  <li key={`${line.raw}-${index}`}>{line.raw}</li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}
    </main>
  )
}

export default App
