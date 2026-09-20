package LeaseBook;

our %HANDLES;

sub LeaseBook::claim {
    my ($self, $id, $owner) = @_;
    $HANDLES{$id} = { owner => $owner, state => "held" };
}

sub LeaseBook::release {
    my ($self, $id) = @_;
    $HANDLES{$id}{state} = "released";
    $self->close_handle($id);
}

package LeaseReaper;

sub LeaseReaper::expire {
    my ($self, $id) = @_;
    $LeaseBook::HANDLES{$id}{state} = "expired";
    $self->close_handle($id);
}

sub close_handle {
    my ($self, $id) = @_;
    return $id;
}

package LeaseBook;

sub LeaseBook::close_handle {
    my ($self, $id) = @_;
    return $id;
}

1;
