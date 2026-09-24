import { BuyerFlow } from "./components/BuyerFlow";

const EVENT_ID = import.meta.env.VITE_EVENT_ID as string | undefined;

function App() {
  return (
    <div className="app">
      <header className="app__header">
        <h1>TicketRush</h1>
        <p>Pick your seats, hold them, and check out.</p>
      </header>

      {EVENT_ID ? (
        <BuyerFlow eventId={EVENT_ID} />
      ) : (
        <p className="status-line status-line--error">
          VITE_EVENT_ID is not set. Copy frontend/.env.example to frontend/.env.local and set it to a
          real event id (see frontend/README.md for how to seed one).
        </p>
      )}
    </div>
  );
}

export default App;
