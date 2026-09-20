package Batch;

sub Batch::convert {
    my ($items, $write) = @_;
    my @completed;
    for my $item (@$items) {
        my $ok = eval {
            $write->($item);
            1;
        };
        unless ($ok) {
            return {
                items => \@completed,
                complete => 0,
                next_index => scalar @completed,
            };
        }
        push @completed, $item;
    }
    return {
        items => \@completed,
        complete => 1,
        next_index => scalar @completed,
    };
}

1;
