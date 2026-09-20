package Parcel;

sub _enqueue {
    my ($self, $job) = @_;
    my $job_id = $self->{next_job_id}++;
    $self->{queued}{$job_id} = {
        job => $job,
        state => "queued",
        attempt => 0,
    };
    return $job_id;
}

sub _schedule {
    my ($registry, $clock, $job_id, $delay) = @_;
    my $timer_id = "retry-$job_id";
    my $timer = {
        id => $timer_id,
        active => 1,
        due_at => $clock->now() + $delay,
    };
    $registry->{timers}{$timer_id} = $timer;
    return $timer;
}

sub _run_until_done {
    my ($self, $job_id, $clock, $timer) = @_;
    my $backoff = $self->{base_delay};
    while (1) {
        my $entry = $self->{queued}{$job_id};
        $entry->{attempt}++;
        my $outcome = $self->{runner}->($entry->{job}, $entry->{attempt});
        return $outcome->{value} if $outcome->{ok};
        return undef if !$outcome->{retryable} || $entry->{attempt} >= $self->{max_attempts};
        $timer->{due_at} = $clock->now() + $backoff;
        $clock->sleep_until($timer->{due_at});
        return undef if !$timer->{active};
        $backoff *= 2;
    }
}

sub _cancel {
    my ($self, $registry, $job_id, $timer) = @_;
    $timer->{active} = 0;
    delete $registry->{timers}{$timer->{id}};
    delete $self->{queued}{$job_id};
}

sub Parcel::receive {
    my ($self, $job, $clock, $registry) = @_;
    my $job_id = _enqueue($self, $job);
    my $timer = _schedule($registry, $clock, $job_id, $self->{base_delay});
    my $result = _run_until_done($self, $job_id, $clock, $timer);
    _cancel($self, $registry, $job_id, $timer);
    return $result;
}

1;
