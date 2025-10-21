// RAID Management WebUI - Frontend JavaScript

let raidTypes = [];
let physicalDrives = [];
let deleteVdId = null;

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    loadControllerInfo();
    loadPhysicalDrives();
    loadVirtualDrives();
    loadRaidTypes();
    
    // Refresh every 30 seconds
    setInterval(refreshAll, 30000);
});

function refreshAll() {
    loadControllerInfo();
    loadPhysicalDrives();
    loadVirtualDrives();
}

function loadControllerInfo() {
    fetch('/api/controller')
        .then(response => response.json())
        .then(data => {
            document.getElementById('ctrl-model').textContent = data.model;
            document.getElementById('ctrl-serial').textContent = data.serial;
            document.getElementById('ctrl-firmware').textContent = data.firmware;
            
            const statusBadge = document.getElementById('ctrl-status');
            statusBadge.textContent = data.status;
            statusBadge.className = data.status === 'Online' ? 'badge bg-success' : 'badge bg-danger';
        })
        .catch(error => console.error('Error loading controller info:', error));
}

function loadPhysicalDrives() {
    fetch('/api/drives')
        .then(response => response.json())
        .then(data => {
            physicalDrives = data;
            const tbody = document.getElementById('drives-table');
            
            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" class="text-center">No physical drives found</td></tr>';
                return;
            }
            
            tbody.innerHTML = data.map(drive => `
                <tr>
                    <td>${drive.slot}</td>
                    <td>${drive.did}</td>
                    <td><span class="badge ${getStateBadgeClass(drive.state)}">${drive.state}</span></td>
                    <td>${drive.dg}</td>
                    <td>${drive.size}</td>
                    <td>${drive.interface}</td>
                    <td>${drive.media}</td>
                    <td>${drive.model}</td>
                </tr>
            `).join('');
        })
        .catch(error => console.error('Error loading drives:', error));
}

function loadVirtualDrives() {
    fetch('/api/vdrives')
        .then(response => response.json())
        .then(data => {
            const tbody = document.getElementById('vdrives-table');
            
            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No RAID arrays configured</td></tr>';
                return;
            }
            
            tbody.innerHTML = data.map(vd => `
                <tr>
                    <td>${vd.dg_vd}</td>
                    <td><span class="badge bg-primary">${vd.type}</span></td>
                    <td><span class="badge ${getStateBadgeClass(vd.state)}">${vd.state}</span></td>
                    <td>${vd.access}</td>
                    <td>${vd.size}</td>
                    <td><code>${vd.device || 'N/A'}</code></td>
                    <td>
                        <button class="btn btn-danger btn-sm" onclick="showDeleteModal('${vd.dg_vd}')">
                            <i class="bi bi-trash"></i> Delete
                        </button>
                    </td>
                </tr>
            `).join('');
        })
        .catch(error => console.error('Error loading virtual drives:', error));
}

function loadRaidTypes() {
    fetch('/api/raid_types')
        .then(response => response.json())
        .then(data => {
            raidTypes = data;
            const select = document.getElementById('raid-type');
            select.innerHTML = '<option value="">Select RAID type...</option>' +
                data.map(type => `<option value="${type.value}">${type.name}</option>`).join('');
            
            select.addEventListener('change', updateDriveSelection);
        })
        .catch(error => console.error('Error loading RAID types:', error));
}

function updateDriveSelection() {
    const selectedType = document.getElementById('raid-type').value;
    const raidType = raidTypes.find(t => t.value === selectedType);
    
    if (!raidType) {
        document.getElementById('drive-selection').innerHTML = '<p class="text-muted">Please select a RAID type first</p>';
        document.getElementById('raid-type-help').textContent = '';
        return;
    }
    
    document.getElementById('raid-type-help').textContent = `Minimum ${raidType.min_drives} drives required`;
    
    const availableDrives = physicalDrives.filter(d => d.state === 'Unconfigured Good' || d.state === 'UGood');
    
    if (availableDrives.length === 0) {
        document.getElementById('drive-selection').innerHTML = '<p class="text-warning">No unconfigured drives available</p>';
        return;
    }
    
    document.getElementById('drive-selection').innerHTML = availableDrives.map(drive => `
        <div class="form-check">
            <input class="form-check-input" type="checkbox" value="${drive.slot}" id="drive-${drive.slot}">
            <label class="form-check-label" for="drive-${drive.slot}">
                <strong>${drive.slot}</strong> - ${drive.size} ${drive.model}
            </label>
        </div>
    `).join('');
}

function createRaid() {
    const raidType = document.getElementById('raid-type').value;
    if (!raidType) {
        alert('Please select a RAID type');
        return;
    }
    
    const selectedDrives = Array.from(document.querySelectorAll('#drive-selection input:checked'))
        .map(cb => cb.value);
    
    if (selectedDrives.length === 0) {
        alert('Please select at least one drive');
        return;
    }
    
    const raidTypeInfo = raidTypes.find(t => t.value === raidType);
    if (selectedDrives.length < raidTypeInfo.min_drives) {
        alert(`${raidType.toUpperCase()} requires at least ${raidTypeInfo.min_drives} drives`);
        return;
    }
    
    if (!confirm(`Create ${raidType.toUpperCase()} array with ${selectedDrives.length} drives?`)) {
        return;
    }
    
    // Show loading
    const btn = event.target;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Creating...';
    
    fetch('/api/create_raid', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            type: raidType,
            drives: selectedDrives
        })
    })
    .then(response => response.json())
    .then(data => {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-check-circle"></i> Create RAID';
        
        if (data.success) {
            alert('RAID array created successfully!');
            bootstrap.Modal.getInstance(document.getElementById('createRaidModal')).hide();
            refreshAll();
        } else {
            alert('Failed to create RAID: ' + (data.error || data.output));
        }
    })
    .catch(error => {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-check-circle"></i> Create RAID';
        alert('Error creating RAID: ' + error);
    });
}

function showDeleteModal(vdId) {
    deleteVdId = vdId;
    document.getElementById('delete-vd-id').textContent = vdId;
    new bootstrap.Modal(document.getElementById('deleteRaidModal')).show();
}

function confirmDelete() {
    if (!deleteVdId) return;
    
    const btn = event.target;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Deleting...';
    
    fetch('/api/delete_raid', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({vd_id: deleteVdId})
    })
    .then(response => response.json())
    .then(data => {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-trash"></i> Delete RAID';
        
        if (data.success) {
            alert('RAID array deleted successfully');
            bootstrap.Modal.getInstance(document.getElementById('deleteRaidModal')).hide();
            refreshAll();
        } else {
            alert('Failed to delete RAID: ' + (data.error || data.output));
        }
    })
    .catch(error => {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-trash"></i> Delete RAID';
        alert('Error deleting RAID: ' + error);
    });
}

function getStateBadgeClass(state) {
    const stateUpper = state.toUpperCase();
    if (stateUpper.includes('OPTL') || stateUpper.includes('ONLN') || stateUpper.includes('OPTIMAL') || stateUpper.includes('ONLINE')) return 'bg-success';
    if (stateUpper.includes('UGOOD') || stateUpper.includes('UNCONFIGURED')) return 'bg-info';
    if (stateUpper.includes('DGRD') || stateUpper.includes('PDGD')) return 'bg-warning';
    if (stateUpper.includes('OFFLN') || stateUpper.includes('FAIL')) return 'bg-danger';
    return 'bg-secondary';
}

function loadHealth() {
    const tbody = document.getElementById('health-table');
    tbody.innerHTML = '<tr><td colspan="6" class="text-center"><span class="spinner-border spinner-border-sm"></span> Scanning drives...</td></tr>';
    
    fetch('/api/health')
        .then(response => response.json())
        .then(data => {
            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="text-center">No drive health data available</td></tr>';
                return;
            }
            
            tbody.innerHTML = data.map(drive => {
                const statusClass = drive.overall_health === 'Good' ? 'text-success' : 'text-warning';
                const tempClass = parseInt(drive.temperature) > 50 ? 'text-danger' : 'text-success';
                
                return `
                    <tr>
                        <td>${drive.slot}</td>
                        <td class="${statusClass}"><strong>${drive.status}</strong></td>
                        <td class="${tempClass}">${drive.temperature}</td>
                        <td>${drive.power_on_hours}</td>
                        <td>${drive.media_errors === '0' ? '<span class="text-success">0</span>' : '<span class="text-danger">' + drive.media_errors + '</span>'}</td>
                        <td>${drive.predictive_failures === '0' ? '<span class="text-success">0</span>' : '<span class="text-danger">' + drive.predictive_failures + '</span>'}</td>
                    </tr>
                `;
            }).join('');
        })
        .catch(error => {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center text-danger">Error loading health data</td></tr>';
            console.error('Error loading health:', error);
        });
}

function runSpeedTest() {
    const device = document.getElementById('test-device').value;
    const testType = document.getElementById('test-type').value;
    const resultsDiv = document.getElementById('speed-test-results');
    const btn = event.target;
    
    // Show results div and reset
    resultsDiv.style.display = 'block';
    document.getElementById('read-speed').innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    document.getElementById('read-iops').innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    document.getElementById('write-speed').innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    document.getElementById('write-iops').innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    
    // Disable button
    btn.disabled = true;
    const originalText = btn.innerHTML;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Testing...';
    
    fetch('/api/speed_test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            device: device,
            type: testType
        })
    })
    .then(response => response.json())
    .then(data => {
        document.getElementById('read-speed').textContent = data.read_speed;
        document.getElementById('read-iops').textContent = data.read_iops;
        document.getElementById('write-speed').textContent = data.write_speed;
        document.getElementById('write-iops').textContent = data.write_iops;
        
        btn.disabled = false;
        btn.innerHTML = originalText;
        
        if (data.status !== 'Completed') {
            alert('Test completed with status: ' + data.status);
        }
    })
    .catch(error => {
        alert('Error running speed test: ' + error);
        btn.disabled = false;
        btn.innerHTML = originalText;
    });
}

// Modal event listeners
document.getElementById('createRaidModal').addEventListener('shown.bs.modal', function() {
    loadPhysicalDrives();
    updateDriveSelection();
});
