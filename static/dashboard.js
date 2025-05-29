// Global state for tracking hashrate history
let hashrateHistory = {
  labels: [],
  datasets: []
};

// Global chart instances
let hashrateChart = null;
let shareChart = null;

// Constants
const REFRESH_INTERVAL = 10000; // 10 seconds
const MAX_HISTORY_POINTS = 20;
const COLORS = [
  '#00B9B9', '#1ADABB', '#4CD4FF', '#3498db', '#9b59b6', 
  '#e74c3c', '#f1c40f', '#34495e', '#d35400', '#7f8c8d'
];

// Initialize the dashboard
document.addEventListener('DOMContentLoaded', function() {
  // Set Chart.js defaults to match theme
  Chart.defaults.color = '#EEEEEE';
  Chart.defaults.borderColor = '#333333';
  Chart.defaults.font.family = "'Inter', -apple-system, BlinkMacSystemFont, sans-serif";
  
  initCharts();
  updateData();
  
  // Set up refresh interval
  setInterval(updateData, REFRESH_INTERVAL);
});

// Initialize Chart.js charts
function initCharts() {
  // Hashrate chart
  const hashrateCtx = document.getElementById('hashrateChart').getContext('2d');
  hashrateChart = new Chart(hashrateCtx, {
    type: 'line',
    data: {
      labels: [],
      datasets: []
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: {
        duration: 1000
      },
      scales: {
        x: {
          grid: {
            color: 'rgba(255, 255, 255, 0.05)',
            borderColor: '#333333'
          },
          ticks: {
            color: '#EEEEEE'
          }
        },
        y: {
          beginAtZero: true,
          grid: {
            color: 'rgba(255, 255, 255, 0.05)',
            borderColor: '#333333'
          },
          ticks: {
            color: '#EEEEEE'
          },
          title: {
            display: true,
            text: 'Hashrate',
            color: '#EEEEEE'
          }
        }
      },
      plugins: {
        legend: {
          position: 'top',
          labels: {
            color: '#EEEEEE',
            usePointStyle: true,
            padding: 20
          }
        },
        tooltip: {
          backgroundColor: 'rgba(30, 30, 30, 0.9)',
          titleColor: '#FFFFFF',
          bodyColor: '#EEEEEE',
          borderColor: '#333333',
          borderWidth: 1,
          padding: 12,
          callbacks: {
            label: function(context) {
              let label = context.dataset.label || '';
              if (label) {
                label += ': ';
              }
              if (context.parsed.y !== null) {
                label += formatHashrate(context.parsed.y);
              }
              return label;
            }
          }
        }
      }
    }
  });
  
  // Shares chart
  const sharesCtx = document.getElementById('sharesChart').getContext('2d');
  shareChart = new Chart(sharesCtx, {
    type: 'doughnut',
    data: {
      labels: ['Accepted', 'Rejected'],
      datasets: [{
        data: [0, 0],
        backgroundColor: [
          'rgba(26, 218, 187, 0.8)',
          'rgba(255, 76, 91, 0.8)'
        ],
        borderColor: [
          '#1ADABB',
          '#FF4C5B'
        ],
        borderWidth: 1
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: {
        duration: 1000
      },
      cutout: '70%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: '#EEEEEE',
            usePointStyle: true,
            padding: 15
          }
        },
        tooltip: {
          backgroundColor: 'rgba(30, 30, 30, 0.9)',
          titleColor: '#FFFFFF',
          bodyColor: '#EEEEEE',
          borderColor: '#333333',
          borderWidth: 1,
          padding: 12
        }
      }
    }
  });
}

// Fetch data from API and update dashboard
async function updateData() {
  try {
    // Fetch miners stats
    const statsResponse = await fetch('/api/stats');
    const minerStats = await statsResponse.json();
    
    if (!minerStats || !Array.isArray(minerStats)) {
      console.error('Invalid miners data format:', minerStats);
      return;
    }
    
    const poolsResponse = await fetch('/api/pools');
    const poolsInfo = await poolsResponse.json();
    
    // Update last refresh time
    document.getElementById('lastRefreshed').textContent = new Date().toLocaleTimeString();
    
    // Update pools information
    updatePoolsInfo(poolsInfo);
    
    // Calculate summary statistics
    updateSummaryStats(minerStats);
    
    // Add new data point to hashrate history
    addHashrateDataPoint(minerStats);
    
    // Update shares chart
    updateSharesChart(minerStats);
    
    // Update miners table
    updateMinersTable(minerStats);
    
  } catch (error) {
    console.error('Error fetching stats:', error);
  }
}

// Update pools information
function updatePoolsInfo(poolsInfo) {
  const container = document.getElementById('poolsContainer');
  container.innerHTML = '';
  
  // Create a card for each pool
  for (const [poolName, poolData] of Object.entries(poolsInfo)) {
    const card = document.createElement('div');
    card.className = 'pool-card';
    
    const isActive = poolData.connected_miners > 0;
    
    card.innerHTML = `
      <div class="pool-header">
        <h3>${poolName.toUpperCase()}</h3>
        <span class="pool-status ${isActive ? 'active' : 'inactive'}">
          ${isActive ? 'Active' : 'Idle'}
        </span>
      </div>
      <div class="pool-details">
        <div class="pool-detail">
          <span class="detail-label">Address:</span>
          <span class="detail-value">${poolData.host}:${poolData.port}</span>
        </div>
        <div class="pool-detail">
          <span class="detail-label">Proxy Port:</span>
          <span class="detail-value">${poolData.proxy_port}</span>
        </div>
        <div class="pool-detail">
          <span class="detail-label">User:</span>
          <span class="detail-value">${poolData.user}</span>
        </div>
        <div class="pool-stats">
          <div class="pool-stat">
            <span class="stat-value">${poolData.connected_miners}</span>
            <span class="stat-label">Miners</span>
          </div>
          <div class="pool-stat">
            <span class="stat-value">${formatHashrate(poolData.total_hashrate)}</span>
            <span class="stat-label">Hashrate</span>
          </div>
          <div class="pool-stat">
            <span class="stat-value">${poolData.total_accepted}</span>
            <span class="stat-label">Accepted</span>
          </div>
        </div>
      </div>
    `;
    
    container.appendChild(card);
  }
}

// Update summary statistics cards
function updateSummaryStats(data) {
  // Calculate total hashrate
  const totalHashrate = data.reduce((sum, miner) => sum + miner.hashrate, 0);
  document.getElementById('totalHashrate').textContent = formatHashrate(totalHashrate);
  
  // Calculate total miners
  document.getElementById('activeMiners').textContent = data.length;
  
  // Calculate total shares
  const totalAccepted = data.reduce((sum, miner) => sum + miner.accepted, 0);
  const totalRejected = data.reduce((sum, miner) => sum + miner.rejected, 0);
  document.getElementById('totalShares').textContent = totalAccepted + totalRejected;
  
  // Calculate acceptance rate
  const acceptanceRate = totalAccepted + totalRejected === 0 ? 
    100 : (totalAccepted / (totalAccepted + totalRejected) * 100).toFixed(2);
  document.getElementById('acceptanceRate').textContent = acceptanceRate + '%';
}

// Add new data point to hashrate history
function addHashrateDataPoint(data) {
  // Add new timestamp
  const now = new Date();
  const timeString = now.getHours().toString().padStart(2, '0') + ':' +
                     now.getMinutes().toString().padStart(2, '0') + ':' +
                     now.getSeconds().toString().padStart(2, '0');
  
  // If we have too many points, remove the oldest
  if (hashrateHistory.labels.length >= MAX_HISTORY_POINTS) {
    hashrateHistory.labels.shift();
    hashrateHistory.datasets.forEach(dataset => dataset.data.shift());
  }
  
  // Add new label
  hashrateHistory.labels.push(timeString);
  
  // Process each miner
  data.forEach((miner, index) => {
    const existingDataset = hashrateHistory.datasets.find(d => d.label === miner.worker || d.label === miner.miner);
    
    if (existingDataset) {
      // Update existing dataset
      existingDataset.data.push(miner.hashrate);
    } else {
      // Create new dataset
      const color = COLORS[index % COLORS.length];
      const newDataset = {
        label: miner.worker || miner.miner,
        data: Array(hashrateHistory.labels.length - 1).fill(0).concat([miner.hashrate]),
        borderColor: color,
        backgroundColor: color + '20',
        tension: 0.4,
        fill: false,
        pointRadius: 2,
        borderWidth: 2
      };
      hashrateHistory.datasets.push(newDataset);
    }
  });
  
  // Remove datasets for miners that are no longer connected
  const activeMiners = data.map(m => m.worker || m.miner);
  hashrateHistory.datasets = hashrateHistory.datasets.filter(ds => 
    activeMiners.includes(ds.label));
  
  // Update chart
  hashrateChart.data.labels = hashrateHistory.labels;
  hashrateChart.data.datasets = hashrateHistory.datasets;
  hashrateChart.update();
}

// Update shares chart
function updateSharesChart(data) {
  const totalAccepted = data.reduce((sum, miner) => sum + miner.accepted, 0);
  const totalRejected = data.reduce((sum, miner) => sum + miner.rejected, 0);
  
  shareChart.data.datasets[0].data = [totalAccepted, totalRejected];
  shareChart.update();
}

// Update miners table
function updateMinersTable(data) {
  const tbody = document.getElementById('minersTable').querySelector('tbody');
  tbody.innerHTML = '';
  
  data.forEach(miner => {
    const row = document.createElement('tr');
    
    // Worker/Miner name
    const nameCell = document.createElement('td');
    const fullName = miner.worker || miner.miner;
    nameCell.textContent = truncateMiner(fullName);
    nameCell.title = fullName; // Show full name on hover
    row.appendChild(nameCell);
    
    // Pool
    const poolCell = document.createElement('td');
    poolCell.textContent = miner.pool || 'unknown';
    row.appendChild(poolCell);
    
    // Hashrate
    const hashrateCell = document.createElement('td');
    hashrateCell.textContent = formatHashrate(miner.hashrate);
    row.appendChild(hashrateCell);
    
    // Accepted shares
    const acceptedCell = document.createElement('td');
    acceptedCell.textContent = miner.accepted;
    row.appendChild(acceptedCell);
    
    // Rejected shares with breakdown on hover
    const rejectedCell = document.createElement('td');
    rejectedCell.style.position = 'relative';
    rejectedCell.style.cursor = 'help';
    rejectedCell.textContent = miner.rejected;
    
    // Add tooltip if there are rejections
    if (miner.rejected > 0 && miner.rejected_breakdown) {
      const tooltip = document.createElement('div');
      tooltip.className = 'rejection-tooltip';
      tooltip.innerHTML = `
        <div>Stale: ${miner.rejected_breakdown.stale}</div>
        <div>Duplicate: ${miner.rejected_breakdown.duplicate}</div>
        <div>Low Diff: ${miner.rejected_breakdown.low_diff}</div>
        <div>Other: ${miner.rejected_breakdown.other}</div>
      `;
      rejectedCell.appendChild(tooltip);
    }
    
    row.appendChild(rejectedCell);
    
    // Acceptance rate
    const rateCell = document.createElement('td');
    const rate = miner.accepted + miner.rejected === 0 ? 
      100 : (miner.accepted / (miner.accepted + miner.rejected) * 100).toFixed(2);
    
    const badge = document.createElement('span');
    badge.classList.add('badge');
    badge.classList.add(rate >= 95 ? 'badge-success' : 'badge-danger');
    badge.textContent = rate + '%';
    
    rateCell.appendChild(badge);
    row.appendChild(rateCell);
    
    // Pool requested difficulty
    const poolDiffCell = document.createElement('td');
    poolDiffCell.textContent = formatDifficulty(miner.pool_difficulty || 0);
    poolDiffCell.title = (miner.pool_difficulty || 0).toFixed(2); // Show exact value on hover
    row.appendChild(poolDiffCell);
    
    // Miner difficulty (effective)
    const difficultyCell = document.createElement('td');
    difficultyCell.textContent = formatDifficulty(miner.difficulty);
    difficultyCell.title = miner.difficulty.toFixed(2); // Show exact value on hover
    row.appendChild(difficultyCell);
    
    // Last share difficulty
    const lastShareCell = document.createElement('td');
    if (miner.last_share_difficulty > 0) {
      lastShareCell.textContent = formatDifficulty(miner.last_share_difficulty);
      lastShareCell.title = miner.last_share_difficulty.toFixed(2); // Show exact value on hover
      
      // Highlight if last share was better than pool difficulty
      if (miner.last_share_difficulty > miner.difficulty * 1.1) {
        lastShareCell.style.color = 'var(--secondary)';
        lastShareCell.style.fontWeight = 'bold';
      }
    } else {
      lastShareCell.textContent = '-';
    }
    row.appendChild(lastShareCell);
    
    // Highest difficulty
    const highestCell = document.createElement('td');
    if (miner.highest_difficulty > 0) {
      highestCell.textContent = formatDifficulty(miner.highest_difficulty);
      highestCell.title = miner.highest_difficulty.toFixed(2); // Show exact value on hover
      
      // Always highlight the highest difficulty
      highestCell.style.color = 'var(--accent)';
      highestCell.style.fontWeight = 'bold';
    } else {
      highestCell.textContent = '-';
    }
    row.appendChild(highestCell);
    
    tbody.appendChild(row);
  });
}

// Helper function to format hashrate
function formatHashrate(hashrate) {
  if (hashrate >= 1e18) {
    return (hashrate / 1e18).toFixed(2) + ' EH/s';
  } else if (hashrate >= 1e15) {
    return (hashrate / 1e15).toFixed(2) + ' PH/s';
  } else if (hashrate >= 1e12) {
    return (hashrate / 1e12).toFixed(2) + ' TH/s';
  } else if (hashrate >= 1e9) {
    return (hashrate / 1e9).toFixed(2) + ' GH/s';
  } else if (hashrate >= 1e6) {
    return (hashrate / 1e6).toFixed(2) + ' MH/s';
  } else if (hashrate >= 1e3) {
    return (hashrate / 1e3).toFixed(2) + ' KH/s';
  } else {
    return hashrate.toFixed(2) + ' H/s';
  }
}

// Helper function to format difficulty with K, M, B suffixes
function formatDifficulty(diff) {
  if (!diff || diff === 0) return '0';
  
  if (diff >= 1e12) {
    return (diff / 1e12).toFixed(2) + 'T';
  } else if (diff >= 1e9) {
    return (diff / 1e9).toFixed(2) + 'B';
  } else if (diff >= 1e6) {
    return (diff / 1e6).toFixed(2) + 'M';
  } else if (diff >= 1e3) {
    return (diff / 1e3).toFixed(2) + 'K';
  } else {
    return diff.toFixed(2);
  }
}

// Helper function to truncate long miner names
function truncateMiner(minerName) {
  if (!minerName) return '-';
  
  // If the name is short enough, return as-is
  if (minerName.length <= 27) {
    return minerName;
  }
  
  // Take first 12 and last 12 characters with ... in between
  return minerName.substring(0, 12) + '...' + minerName.substring(minerName.length - 12);
} 