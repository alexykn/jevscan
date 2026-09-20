package Ledger;

sub _amount_for {
    my ($record) = @_;
    return sum_lines($record->{lines});
}

sub _publish {
    my ($self, $record, $amount) = @_;
    $self->write_total($record->{id}, $amount);
    $self->audit({ id => $record->{id}, amount => $amount });
    $self->notify($record->{owner}, $amount);
}

sub Ledger::reconcile {
    my ($self, $payload) = @_;
    my $record = $self->load($payload->{id});
    return undef unless $record;
    my $amount = _amount_for($record);
    _publish($self, $record, $amount);
    return $amount;
}

1;
