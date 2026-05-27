import React from 'react';

function App() {
  return (
    <div className="App">
      <header className="App-header">
        <h1>Suffolk Leads Dashboard</h1>
        <p>Welcome to the Suffolk County real estate leads management system.</p>
      </header>
      <main>
        <section>
          <h2>Lead Statistics</h2>
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th>Count</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>New</td>
                <td>0</td>
              </tr>
              <tr>
                <td>Processed</td>
                <td>0</td>
              </tr>
            </tbody>
          </table>
        </section>
      </main>
    </div>
  );
}

export default App;
