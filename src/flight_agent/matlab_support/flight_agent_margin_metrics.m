function result = flight_agent_margin_metrics(loop)
result = struct('kind','stability_margins','execution_status','completed', ...
    'valid',false,'metrics',struct,'diagnostics',{{}},'details',struct, ...
    'artifact_paths',{{}});
if size(loop,1) ~= 1 || size(loop,2) ~= 1 || ndims(loop) > 2
    result.diagnostics = {'A single SISO open-loop model is required for D1 margins.'};
    return;
end
margins = allmargin(loop);
gain = margins.GainMargin(:);
gainFrequency = margins.GMFrequency(:);
phase = margins.PhaseMargin(:);
phaseFrequency = margins.PMFrequency(:);
usedFallback = false;
if isempty(gain) || isempty(phase)
    [gm,pm,wcg,wcp] = margin(loop);
    if isempty(gain)
        gain = gm;
        gainFrequency = wcg;
    end
    if isempty(phase)
        phase = pm;
        phaseFrequency = wcp;
    end
    usedFallback = true;
end
result.details = struct('allmargin',margins,'margin_fallback',usedFallback, ...
    'closed_loop_stable',margins.Stable,'frequency_unit','rad/s', ...
    'gain_crossings',{{}},'phase_crossings',{{}});
if isempty(gain) || isempty(phase) || any(isnan(gain)) || any(gain < 0) || any(isnan(phase))
    result.diagnostics = {'No defined gain/phase margin is available for the registered loop.'};
    return;
end
gainDb = 20*log10(gain);
for index = 1:numel(gainDb)
    result.details.gain_crossings{end+1} = struct( ...
        'margin',metric(gainDb(index),'dB'),'frequency',gainFrequency(index));
end
for index = 1:numel(phase)
    result.details.phase_crossings{end+1} = struct( ...
        'margin',metric(phase(index),'deg'),'frequency',phaseFrequency(index));
end
result.metrics = struct('gain_margin',metric(min(gainDb),'dB'), ...
    'phase_margin',metric(min(phase),'deg'));
result.valid = true;
result.diagnostics = { ...
    'All registered crossings are retained; the least margin is used for each criterion.', ...
    'Margin threshold compliance is not a claim of sufficient robustness or full-envelope stability.'};
if ~isequal(margins.Stable,1)
    result.diagnostics{end+1} = 'allmargin does not establish a stable nominal closed loop.';
end
end

function value = metric(number,unit)
value = struct('value',number,'unit',unit);
if isinf(number)
    value.value = NaN;
    if number > 0
        value.special = 'positive_infinity';
        value.reason = 'Unbounded margin reported for a loop without the corresponding crossover.';
    else
        value.special = 'negative_infinity';
        value.reason = 'Zero gain multiplier corresponds to negative infinite dB.';
    end
end
end
