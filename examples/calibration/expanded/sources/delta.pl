package Parcel;

sub _attempt {
    my ($self, $job, $attempt) = @_;
    my $outcome = $self->{runner}->($job, $attempt);
    return { ok => 1, value => $outcome->{value} } if $outcome->{ok};
    return {
        ok => 0,
        retryable => $outcome->{retryable},
        error => $outcome->{error},
    };
}

sub Parcel::receive {
    my ($self, $job, $clock, $registry) = @_;
    my $job_id = $self->{next_job_id}++;
    my $attempt = 0;
    my $backoff = $self->{base_delay};
    my $timer_id = "retry-$job_id";
    my $timer = {
        id => $timer_id,
        active => 1,
        due_at => $clock->now(),
    };
    $registry->{timers}{$timer_id} = $timer;
    $self->{active}{$job_id} = {
        job => $job,
        attempt => 0,
        next_at => $timer->{due_at},
    };
    while (1) {
        $attempt++;
        $self->{active}{$job_id}{attempt} = $attempt;
        my $outcome = _attempt($self, $job, $attempt);
        if ($outcome->{ok}) {
            delete $registry->{timers}{$timer_id};
            delete $self->{active}{$job_id};
            return $outcome->{value};
        }
        if (!$outcome->{retryable} || $attempt >= $self->{max_attempts}) {
            $timer->{active} = 0;
            delete $registry->{timers}{$timer_id};
            delete $self->{active}{$job_id};
            die $outcome->{error};
        }
        if ($self->{cancelled}{$job_id}) {
            $timer->{active} = 0;
            delete $registry->{timers}{$timer_id};
            delete $self->{active}{$job_id};
            return undef;
        }
        $timer->{due_at} = $clock->now() + $backoff;
        $timer->{active} = 1;
        $self->{active}{$job_id}{next_at} = $timer->{due_at};
        $clock->sleep_until($timer->{due_at});
        if (!$timer->{active} || !exists $registry->{timers}{$timer_id}) {
            delete $self->{active}{$job_id};
            return undef;
        }
        $backoff *= 2;
    }
}

1;
