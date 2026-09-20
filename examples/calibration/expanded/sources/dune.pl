package Ledger;

sub Ledger::reconcile {
    my ($self, $payload) = @_;
    my $record = $self->load($payload->{id});
    return undef unless $record;
    my $amount = 0;
    for my $line (@{$record->{lines}}) {
        $amount += $line->{units} * $line->{rate};
    }
    $self->write_total($record->{id}, $amount);
    $self->audit({ id => $record->{id}, amount => $amount });
    $self->notify($record->{owner}, $amount);
    return $amount;
}

1;
