function result = flight_agent_step_metrics(inputTime,inputValue,responseTime,responseValue,policy,settlingBand)
if nargin < 6
    settlingBand = 0.05;
end
assert(isnumeric(settlingBand) && isscalar(settlingBand) && isfinite(settlingBand) ...
    && settlingBand > 0 && settlingBand < 1, 'Invalid finite settling band');
result = struct('kind','step_response','execution_status','completed', ...
    'valid',false,'metrics',struct,'diagnostics',{{}},'details',struct, ...
    'artifact_paths',{{}});
defaults = struct('tail_fraction',0.2,'minimum_window_seconds',1.0, ...
    'maximum_relative_range',0.02,'maximum_relative_drift',0.01);
names = fieldnames(defaults);
for index = 1:numel(names)
    if ~isfield(policy,names{index})
        policy.(names{index}) = defaults.(names{index});
    end
end
result.details = struct('settling_band',settlingBand,'steady_state_policy',policy);
t = double(inputTime(:));
u = double(inputValue(:));
ty = double(responseTime(:));
y = double(responseValue(:));
if numel(t) ~= numel(u) || numel(ty) ~= numel(y) || numel(t) < 4 || numel(ty) < 4 ...
        || any(~isfinite([t;u;ty;y])) || any(diff(t) <= 0) || any(diff(ty) <= 0)
    result.diagnostics = {'Signals require finite scalar data and strictly increasing time axes.'};
    return;
end
timeTolerance = 100*eps(max(1,max(abs([t;ty]))));
if ty(1) < t(1)-timeTolerance || ty(end) > t(end)+timeTolerance
    result.diagnostics = {'Input and response time axes cannot be aligned without extrapolation.'};
    return;
end
changes = find(abs(diff(u)) > max(1e-10,1e-8*max(abs(u))));
if numel(changes) ~= 1
    result.diagnostics = {'A single identifiable input step is required.'};
    return;
end
stepTime = t(changes(1)+1);
before = find(ty < stepTime);
after = find(ty >= stepTime);
if numel(before) < 2 || numel(after) < 4
    result.diagnostics = {'Both pre-step initial data and a post-step response are required.'};
    return;
end
elapsed = ty(after)-stepTime;
duration = elapsed(end);
window = max(policy.minimum_window_seconds,policy.tail_fraction*duration);
if duration < 2*window
    result.diagnostics = {'Simulation is too short to verify a reliable steady-state window.'};
    return;
end
initial = mean(y(before));
tail = after(ty(after) >= ty(end)-window);
if numel(tail) < 4
    result.diagnostics = {'Not enough samples in the steady-state window.'};
    return;
end
final = mean(y(tail));
change = final-initial;
minimumChange = max(1e-8,100*eps(max(1,max(abs(y)))));
result.details = struct('step_time',stepTime,'input_initial',u(changes(1)), ...
    'input_final',u(changes(1)+1),'initial_value',initial,'final_value',final, ...
    'steady_window_start',ty(tail(1)),'steady_window_seconds',window, ...
    'settling_band',settlingBand,'steady_state_policy',policy,'sample_count',numel(y));
if abs(change) <= minimumChange
    result.diagnostics = {'The response has no reliable nonzero steady-state change.'};
    return;
end
midpoint = floor(numel(tail)/2);
relativeRange = (max(y(tail))-min(y(tail)))/abs(change);
relativeDrift = abs(mean(y(tail(1:midpoint)))-mean(y(tail(midpoint+1:end))))/abs(change);
result.details.relative_steady_range = relativeRange;
result.details.relative_steady_drift = relativeDrift;
if relativeRange > policy.maximum_relative_range ...
        || relativeDrift > policy.maximum_relative_drift ...
        || max(y(before))-min(y(before)) > policy.maximum_relative_range*abs(change)
    result.diagnostics = {'Initial or final response is not reliably steady; no forced step metrics.'};
    return;
end
info = stepinfo(y(after),elapsed,final,initial,'SettlingTimeThreshold',settlingBand);
if ~isfinite(info.Overshoot) || ~isfinite(info.SettlingTime) || info.SettlingTime < 0
    result.diagnostics = {'stepinfo did not produce valid finite response metrics.'};
    return;
end
result.metrics = struct('overshoot',struct('value',info.Overshoot,'unit','%'), ...
    'settling_time',struct('value',info.SettlingTime,'unit','s'));
result.valid = true;
end
